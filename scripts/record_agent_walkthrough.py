#!/usr/bin/env python3
"""Record the agent playbook walkthrough: plan a multi-step request, refine it in words, run it, ask about it.

Starts its own dashboard with simulated receivers (RF_SENTINEL_AGENT_SIMULATE=1, a temporary log directory, no
radio used), drives it with Playwright, and writes docs/media/rf-sentinel-agent-playbook.{mp4,gif}. Needs ffmpeg
and Playwright's Chromium (pass --chromium PATH if it is not in Playwright's default location).
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REQUEST = ("Sweep for bluetooth trackers for 15 seconds. If you find any, watch them for 15 seconds and send an urgent "
           "alert if one is still there after 5 seconds, and record 1 second of IQ. Then survey cell towers for 10 "
           "seconds and alert me if a 2G cell shows up.")
REFINE = "also tag them as suspect"


def wait_for(url: str, timeout_s: float = 40) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except OSError:
            time.sleep(0.5)
    raise SystemExit(f"dashboard did not start at {url}")


class Frames:
    """Timed screenshots (the same approach as record_walkthrough.py): no browser video codec needed."""

    def __init__(self, page, folder: Path):
        self.page, self.folder, self.times = page, folder, []
        folder.mkdir(parents=True, exist_ok=True)
        self.start = time.monotonic()

    def snap(self) -> None:
        self.page.screenshot(path=str(self.folder / f"{len(self.times):05d}.jpg"), type="jpeg", quality=80)
        self.times.append(time.monotonic() - self.start)

    def hold(self, seconds: float, every: float = 0.3, until=None) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.snap()
            if until is not None and until():
                return
            time.sleep(every)

    def type(self, text: str, chunk: int = 6) -> None:
        for i in range(0, len(text), chunk):
            self.page.keyboard.type(text[i:i + chunk])
            self.snap()


def record(url: str, out_dir: Path, chromium: str | None) -> Path:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(**({"executable_path": chromium} if chromium else {}))
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.goto(url, wait_until="domcontentloaded")
        time.sleep(2)
        page.evaluate("document.getElementById('wf').scrollIntoView()")
        page.click("#wfPlannerSeg button[data-planner=deterministic]")
        f = Frames(page, out_dir)
        f.hold(1.0)
        page.click("#wfText")
        f.type(REQUEST)
        f.hold(0.6)
        page.click("#wfPlanBtn")
        f.hold(4.0)
        page.evaluate("document.getElementById('wfRefineText').scrollIntoView({block: 'center'})")
        page.click("#wfRefineText")
        f.type(REFINE, chunk=3)
        page.keyboard.press("Enter")
        f.hold(4.0)
        page.evaluate("document.getElementById('wf').scrollIntoView()")
        page.click("#wfRunBtn")
        f.hold(1.5)
        page.evaluate("document.getElementById('wfLive').scrollIntoView({block: 'start'})")
        f.hold(90, every=0.5, until=lambda: not page.locator("#wf.running").count())
        f.hold(1.5)
        page.evaluate("document.querySelector('.wf-ask-text').scrollIntoView({block: 'center'})")
        page.click(".wf-ask-text")
        f.type("what alerted?", chunk=3)
        page.keyboard.press("Enter")
        f.hold(4.0)
        (out_dir / "times.txt").write_text("\n".join(f"{t:.3f}" for t in f.times))
        browser.close()
    return out_dir


def encode(frames: Path, media: Path, name: str) -> tuple[Path, Path]:
    times = [float(x) for x in (frames / "times.txt").read_text().split()]
    lines = []
    for index, start in enumerate(times):
        end = times[index + 1] if index + 1 < len(times) else start + 2.0
        lines += [f"file '{index:05d}.jpg'", f"duration {max(end - start, 0.01):.3f}"]
    lines.append(f"file '{len(times) - 1:05d}.jpg'")
    (frames / "concat.txt").write_text("\n".join(lines) + "\n")
    mp4, gif = media / f"{name}.mp4", media / f"{name}.gif"
    base = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(frames / "concat.txt")]
    subprocess.run(base + ["-vf", "fps=15,scale=1280:-2:flags=lanczos", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                           "-crf", "28", "-movflags", "+faststart", str(mp4)], check=True)
    subprocess.run(base + ["-vf", "fps=5,scale=900:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];"
                           "[b][p]paletteuse=dither=bayer:bayer_scale=4", str(gif)], check=True)
    return mp4, gif


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=5071)
    parser.add_argument("--chromium", default=os.getenv("PLAYWRIGHT_CHROMIUM"), help="Chromium executable for Playwright")
    parser.add_argument("--name", default="rf-sentinel-agent-playbook")
    args = parser.parse_args()
    if not shutil.which("ffmpeg"):
        parser.error("ffmpeg is required")
    with tempfile.TemporaryDirectory() as temp:
        env = {**os.environ, "RF_SENTINEL_AGENT_SIMULATE": "1", "RF_SENTINEL_LOG_DIR": temp, "RF_SENTINEL_PORT": str(args.port),
               "RF_SENTINEL_HOST": "127.0.0.1", "RF_SENTINEL_TEXTUAL_CONSOLE": "0", "RF_SENTINEL_PLANNER_MODEL": "none",
               "SDR_GATEWAY_BASE_URL": os.getenv("SDR_GATEWAY_BASE_URL", "http://127.0.0.1:9")}
        server = subprocess.Popen([sys.executable, "-m", "rf_platform.ui"], cwd=ROOT, env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_for(f"http://127.0.0.1:{args.port}/")
            frames = record(f"http://127.0.0.1:{args.port}/", Path(temp) / "frames", args.chromium)
            mp4, gif = encode(frames, ROOT / "docs" / "media", args.name)
        finally:
            server.terminate()
            server.wait(timeout=10)
    for path in (mp4, gif):
        print(f"{path.relative_to(ROOT)}  {path.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
