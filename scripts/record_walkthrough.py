#!/usr/bin/env python3
"""Record the ~50 s operator walkthrough (baseline, new entity, relationship change, review).

Uses the real demo backend and shipped dashboard with the validation scenario fixture, paced
for viewing. Requires Docker, ffmpeg on the host, and the images built by validate_demo.sh
(pass --build to rebuild them). Writes docs/media/rf-sentinel-operator-walkthrough.{mp4,gif}.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.generate_demo_events import build_validation_events

# Seconds into the replay: baseline fills quickly, then each change gets its own scene.
BASELINE_SPACING_S = 0.4
CHANGE_OFFSETS_S = {"new_entity": 13.0, "relationship_change": 25.0,
                    "off_cadence_activity": 37.0, "operator_review": 38.0}


def walkthrough_events():
    events = build_validation_events(2.5, 10)  # minimum fixture: 21 baseline + 4 changes
    for event in events:
        phase = event["scenario_phase"]
        event["offset_s"] = (CHANGE_OFFSETS_S[phase] if phase in CHANGE_OFFSETS_S
                             else event["validation_sequence"] * BASELINE_SPACING_S)
    return events


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def encode(frames: Path, media: Path) -> tuple[Path, Path]:
    timeline = json.loads((frames / "timeline.json").read_text())
    concat = frames / "concat.txt"
    lines = []
    for index, start in enumerate(timeline):
        end = timeline[index + 1] if index + 1 < len(timeline) else start + 1.0
        lines += [f"file '{index:05d}.jpg'", f"duration {max(end - start, 0.01):.3f}"]
    lines.append(f"file '{len(timeline) - 1:05d}.jpg'")  # concat demuxer needs the last file repeated
    concat.write_text("\n".join(lines) + "\n")
    mp4 = media / "rf-sentinel-operator-walkthrough.mp4"
    gif = media / "rf-sentinel-operator-walkthrough.gif"
    base = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat)]
    run(base + ["-vf", "fps=15,scale=1280:-2:flags=lanczos", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-crf", "26", "-movflags", "+faststart", str(mp4)])
    run(base + ["-vf", "fps=6,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];"
                "[b][p]paletteuse=dither=bayer:bayer_scale=4", str(gif)])
    return mp4, gif


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true", help="Rebuild the demo and validation images first")
    args = parser.parse_args()
    if not shutil.which("ffmpeg"):
        parser.error("ffmpeg is required on the host")
    work = ROOT / ".demo/walkthrough"
    shutil.rmtree(work, ignore_errors=True)
    frames = work / "frames"; frames.mkdir(parents=True)
    fixture = work / "events.jsonl"
    fixture.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in walkthrough_events()))
    if args.build:
        for dockerfile, tag in [("Dockerfile.demo", "rf-sentinel:public-demo"), ("Dockerfile.validation", "rf-sentinel:validation")]:
            run(["docker", "build", "-q", "-f", dockerfile, "-t", tag, "."], cwd=ROOT, stdout=subprocess.DEVNULL)
    name = "rf-walkthrough-" + uuid.uuid4().hex[:8]
    try:
        run(["docker", "run", "-d", "--name", name, "-e", "RF_SENTINEL_DEMO_LOOP=0",
             "-e", "RF_SENTINEL_DEMO_START_PAUSED=1", "-e", "RF_SENTINEL_DEMO_REFERENCE_TIMESTAMP=1795348800",
             "-v", f"{fixture}:/app/.demo/events/public-demo-events.jsonl:ro", "rf-sentinel:public-demo"],
            capture_output=True)
        run(["docker", "run", "--rm", "--name", name + "-browser", "--network", f"container:{name}",
             "--user", f"{subprocess.os.getuid()}:{subprocess.os.getgid()}", "-e", "HOME=/tmp",
             "-v", f"{work}:/results", "--entrypoint", "sh", "rf-sentinel:validation", "-c",
             "for i in $(seq 100); do python3 -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/status', timeout=2)\" 2>/dev/null && break; sleep .3; done; "
             "exec python3 scripts/walkthrough_browser.py http://127.0.0.1:8080 /results/frames"])
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    mp4, gif = encode(frames, ROOT / "docs/media")
    for path in (mp4, gif):
        print(f"{path.relative_to(ROOT)}  {path.stat().st_size / 2**20:.1f} MiB")


if __name__ == "__main__":
    raise SystemExit(main())
