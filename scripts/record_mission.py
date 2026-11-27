#!/usr/bin/env python3
"""Record the mission view (/agent) running a request end to end, for a product video.

  python3 scripts/record_mission.py "What's on the air around me?" --name rf-sentinel-discovery
  python3 scripts/record_mission.py "Sweep for bluetooth trackers ..." --name rf-sentinel-mission --sim

Uses a running dashboard (--url, default http://127.0.0.1:5050) and signs in with the operator token
(RF_SENTINEL_TOKEN or ~/.config/rf-sentinel/token) when it needs one. The page runs in recording mode: it types the
request itself, plans, runs, and masks device identifiers. Typing and planning play at real speed; while the
mission collects, frames are taken every --every seconds and shown for --show seconds (a time-lapse whose factor is
printed and belongs in the caption). Writes docs/media/<name>.{mp4,gif}. Without --sim, real receivers are used.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("request")
    parser.add_argument("--name", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:5050")
    parser.add_argument("--sim", action="store_true", help="simulated receivers (the page says SIMULATED)")
    parser.add_argument("--every", type=float, default=3.0, help="seconds between frames while collecting")
    parser.add_argument("--show", type=float, default=0.4, help="seconds each collecting frame is shown")
    parser.add_argument("--max-s", type=float, default=600)
    parser.add_argument("--chromium", default=os.getenv("PLAYWRIGHT_CHROMIUM") or os.path.expanduser(
        "~/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome"))
    args = parser.parse_args()
    if not shutil.which("ffmpeg"):
        parser.error("ffmpeg is required")
    token = os.getenv("RF_SENTINEL_TOKEN") or ""
    if not token and os.path.exists(os.path.expanduser("~/.config/rf-sentinel/token")):
        token = open(os.path.expanduser("~/.config/rf-sentinel/token")).read().strip()
    from playwright.sync_api import sync_playwright
    with tempfile.TemporaryDirectory() as temp, sync_playwright() as p:
        frames = Path(temp)
        shots: list[tuple[Path, float]] = []
        browser = p.chromium.launch(executable_path=args.chromium if os.path.exists(args.chromium) else None)
        page = browser.new_page(viewport={"width": 1280, "height": 800}, color_scheme="dark")
        if token:
            page.request.post(f"{args.url}/api/auth/login", data={"token": token}, headers={"X-RF-Sentinel": "1"})
        query = urllib.parse.urlencode({"demo": "1", "q": args.request, **({"sim": "1"} if args.sim else {})})
        page.goto(f"{args.url}/agent?{query}", wait_until="domcontentloaded")

        def snap(show: float) -> None:
            path = frames / f"{len(shots):05d}.jpg"
            page.screenshot(path=str(path), type="jpeg", quality=82)
            shots.append((path, show))

        def follow(selector: str) -> None:  # keep the interesting part on screen, the way a person would scroll
            page.evaluate(f"(() => {{ const el = document.querySelector('{selector}'); if (el && !el.hidden) "
                          "el.scrollIntoView({block: 'start', behavior: 'instant'}); })()")

        started = time.time()
        while time.time() - started < 12 and page.locator("#liveCard:not([hidden])").count() == 0:  # typing, plan
            if page.locator("#planCard:not([hidden])").count():
                follow("#planCard")
            snap(0.25)
            time.sleep(0.25)
        collecting_started, collected = None, 0
        while time.time() - started < args.max_s:
            if page.locator("#liveCard:not([hidden])").count():
                collecting_started = collecting_started or time.time()
                follow("#liveCard")
            if page.locator("#resultCard:not([hidden])").count():
                break
            snap(args.show)
            collected += 1
            time.sleep(args.every)
        follow("#liveCard")
        for _ in range(6):
            snap(0.5)
            time.sleep(0.3)
        follow("#resultCard")
        for _ in range(8):
            snap(0.5)
            time.sleep(0.3)
        browser.close()
        if collecting_started:
            real = time.time() - collecting_started
            print(f"collection: {real:.0f} s shown in {collected * args.show:.0f} s (about {real / max(1, collected * args.show):.0f}x)")
        lines = []
        for path, show in shots:
            lines += [f"file '{path}'", f"duration {show:.3f}"]
        lines.append(f"file '{shots[-1][0]}'")
        (frames / "concat.txt").write_text("\n".join(lines) + "\n")
        media = ROOT / "docs" / "media"
        base = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(frames / "concat.txt")]
        subprocess.run(base + ["-vf", "fps=20,scale=1280:-2:flags=lanczos", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26",
                               "-movflags", "+faststart", str(media / f"{args.name}.mp4")], check=True)
        subprocess.run(base + ["-vf", "fps=6,scale=900:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];"
                               "[b][p]paletteuse=dither=bayer:bayer_scale=4", str(media / f"{args.name}.gif")], check=True)
    for ext in ("mp4", "gif"):
        path = media / f"{args.name}.{ext}"
        print(f"{path.relative_to(ROOT)}  {path.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
