#!/usr/bin/env python3
"""Capture timestamped frames of the shipped dashboard during the scripted operator scenario.

Runs inside the validation image, in the backend container's network namespace.
Scenes advance on observed UI state (latestStatus), never on assumed timing.
Usage: walkthrough_browser.py <base_url> <frames_dir>
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BEACON = "02:00:00:00:99:01"
STATION = "72:94:11:3B:0C:90"
FIELDLAB = "02:10:7A:00:10:01"

CAPTION_JS = """([step, title, body]) => {
  let el = document.getElementById('walkthroughCaption');
  if (!el) {
    el = document.createElement('div'); el.id = 'walkthroughCaption';
    el.style.cssText = 'position:fixed;left:24px;right:24px;bottom:20px;z-index:99999;padding:14px 20px;' +
      'background:rgba(8,18,28,.92);color:#edf5f9;border:1px solid #2b4a5e;border-radius:10px;' +
      'font:600 20px system-ui;box-shadow:0 6px 24px rgba(0,0,0,.45);pointer-events:none';
    document.body.appendChild(el);
    const badge = document.createElement('div');
    badge.textContent = 'SYNTHETIC REPLAY · NO LIVE RF';
    badge.style.cssText = 'position:fixed;top:12px;right:16px;z-index:99999;padding:4px 10px;border-radius:6px;' +
      'background:#0f3d33;color:#79e6c4;font:700 12px system-ui;letter-spacing:.08em;pointer-events:none';
    document.body.appendChild(badge);
  }
  el.innerHTML = `<span style="color:#79e6c4">${step}</span> &nbsp;${title}` +
    `<div style="font:400 16px system-ui;color:#aabfcd;margin-top:4px">${body}</div>`;
}"""

# The dashboard re-renders on every poll, so this is re-applied before each frame.
HIGHLIGHT_JS = """([needle, openSummary]) => {
  document.querySelectorAll('[data-walkthrough-hl]').forEach(e => { e.style.outline = ''; e.removeAttribute('data-walkthrough-hl'); });
  let scope = document.querySelector('#patternAnalyticsOverlay:not([hidden])') || document.getElementById('identityTableWrap');
  if (openSummary) {
    const row = [...scope.querySelectorAll('details')].find(d => d.querySelector('summary')?.textContent.includes(openSummary));
    if (!row) return false;
    row.open = true; scope = row;
  }
  const hits = [...scope.querySelectorAll('tr, article, li, .wifi-topology-list > *, .analytics-list *')]
    .filter(e => e.textContent.includes(needle) && e.children.length);
  const target = hits.sort((a, b) => a.textContent.length - b.textContent.length)[0];
  if (target) {
    target.style.outline = '3px solid #ffcc4d'; target.style.outlineOffset = '2px';
    target.setAttribute('data-walkthrough-hl', '1');
    const box = target.getBoundingClientRect();
    if (box.top < 0 || box.bottom > innerHeight - 110) target.scrollIntoView({block: 'center', behavior: 'instant'});
  }
  return Boolean(target);
}"""


def main():
    base, frames = sys.argv[1], Path(sys.argv[2])
    frames.mkdir(parents=True, exist_ok=True)
    timeline = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1600, "height": 900}, device_scale_factor=1)
        # The dashboard polls continuously, so wait for its first status rather than network idle.
        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_function("() => typeof latestStatus !== 'undefined' && latestStatus", timeout=30000)
        started = time.monotonic()

        keep = None  # [needle, open-summary] highlight to re-apply each frame

        def frame():
            index = len(timeline)
            if keep:
                page.evaluate(HIGHLIGHT_JS, keep)
            page.screenshot(path=str(frames / f"{index:05d}.jpg"), type="jpeg", quality=85)
            timeline.append(time.monotonic() - started)

        def hold(seconds, until=None, timeout=60):
            """Capture frames for at least `seconds`, and until `until()` holds (bounded)."""
            end = time.monotonic() + seconds
            limit = time.monotonic() + timeout
            while time.monotonic() < end or (until and not until()):
                if time.monotonic() > limit:
                    raise RuntimeError("Scenario state not reached before timeout")
                frame(); time.sleep(0.05)

        def rows():
            return page.evaluate("() => (typeof latestStatus !== 'undefined' && latestStatus?.discovery_table) || []")

        def find(mac):
            return next((r for r in rows() if r.get("mac") == mac or r.get("source_address") == mac), None)

        def caption(step, title, body):
            page.evaluate(CAPTION_JS, [step, title, body])

        def highlight(needle, open_summary=None):
            nonlocal keep
            keep = [needle, open_summary]

        caption("1 / 4", "Baseline", "Recurring entities populate across protocol families from the normal replay path.")
        page.evaluate("""() => fetch('/api/scan/start', {method: 'POST', headers: {'Content-Type': 'application/json'},
                          body: JSON.stringify({mode: 'sentinel', preserve_detections: true})})""")
        page.select_option("#detectionViewSelect", "table")
        hold(11, until=lambda: len(rows()) >= 15)

        page.click("#btleTabBtn"); page.select_option("#detectionViewSelect", "table")
        hold(0.3, until=lambda: find(BEACON) is not None)
        caption("2 / 4", "New entity", "A BLE beacon not present in the baseline appears: “Validation New Beacon”.")
        highlight(BEACON); hold(10)

        page.click("#wifiTabBtn"); page.select_option("#detectionViewSelect", "wifi-map")
        hold(0.3, until=lambda: (find(STATION) or {}).get("bssid") == FIELDLAB)
        caption("3 / 4", "Relationship change",
                "The Telemetry station re-associates from the DemoMesh network to the FieldLab-AP-01 access point.")
        highlight(STATION, "FieldLab-AP-01"); hold(11)

        hold(0.3, until=lambda: float((find(BEACON) or {}).get("last_rssi_dbfs", -120)) > -42)
        keep = None
        page.click("#allTabBtn")
        page.click("#patternAnalyticsBtn")
        caption("4 / 4", "Operator review",
                "Pattern-of-Life flags the new beacon's strong signal for proximity review; baseline recurrence stays intact.")
        highlight("Validation New Beacon"); hold(12)
        browser.close()
    (frames / "timeline.json").write_text(json.dumps(timeline))
    print(json.dumps({"frames": len(timeline), "duration_s": timeline[-1] if timeline else 0}))


if __name__ == "__main__":
    main()
