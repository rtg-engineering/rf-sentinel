#!/usr/bin/env python3
"""Capture frames of the shipped dashboard's Analyst flow: baseline → change → investigate → explain → evidence.

Runs inside the validation image in the backend's network namespace. Scenes advance on observed state,
and captions quote numbers read from the page, never hardcoded results.
Usage: analyst_walkthrough_browser.py <base_url> <frames_dir>
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from walkthrough_browser import CAPTION_JS  # same caption bar and synthetic-replay badge

BASELINE_EVENTS = 21
TOTAL_EVENTS = 25
OUTLINE_JS = """(selector) => {
  document.querySelectorAll('[data-wt-outline]').forEach(e => { e.style.outline = ''; e.removeAttribute('data-wt-outline'); });
  const el = typeof selector === 'string' ? document.querySelector(selector) : null;
  if (el) { el.style.outline = '3px solid #ffcc4d'; el.style.outlineOffset = '2px'; el.setAttribute('data-wt-outline', '1');
            el.scrollIntoView({block: 'center', behavior: 'instant'}); }
  return Boolean(el);
}"""


def main():
    base, frames = sys.argv[1], Path(sys.argv[2])
    frames.mkdir(parents=True, exist_ok=True)
    timeline = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1600, "height": 900}, device_scale_factor=1)
        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_function("() => typeof latestStatus !== 'undefined' && latestStatus", timeout=30000)
        started = time.monotonic()
        caption_args = None

        def frame():
            if caption_args:
                page.evaluate(CAPTION_JS, caption_args)
            page.screenshot(path=str(frames / f"{len(timeline):05d}.jpg"), type="jpeg", quality=85)
            timeline.append(time.monotonic() - started)

        def hold(seconds, until=None, timeout=60):
            end, limit = time.monotonic() + seconds, time.monotonic() + timeout
            while time.monotonic() < end or (until and not until()):
                if time.monotonic() > limit:
                    raise RuntimeError("Scenario state not reached before timeout")
                frame(); time.sleep(0.05)

        def accepted():
            return page.evaluate("() => latestStatus?.validation_metrics?.events_accepted || 0")

        def caption(step, title, body):
            nonlocal caption_args
            caption_args = [step, title, body]

        caption("1 / 5", "Baseline", "Normal activity populates the dashboard from the standard replay path.")
        page.evaluate("""() => fetch('/api/scan/start', {method: 'POST', headers: {'Content-Type': 'application/json'},
                          body: JSON.stringify({mode: 'sentinel', preserve_detections: true})})""")
        page.select_option("#detectionViewSelect", "table")
        hold(5, until=lambda: accepted() >= BASELINE_EVENTS)
        page.click("#analystBtn")
        hold(1)
        page.click("#analystBaselineBtn")
        page.wait_for_function("() => !document.getElementById('analystBaselineInfo').textContent.startsWith('No baseline')")
        caption("1 / 5", "Baseline captured", "The Analyst snapshots exactly what the dashboard shows. Nothing changes in collection.")
        hold(4)
        page.click("#analystClose")

        caption("2 / 5", "Change", "New activity arrives while monitoring continues.")
        hold(7, until=lambda: accepted() >= TOTAL_EVENTS)

        page.click("#analystBtn")
        page.click("#analystRunBtn")
        page.wait_for_selector("#analystContent .analyst-finding")
        stats = page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('#analystContent .analytics-stat')]
                                  .map(s => [s.querySelector('span').textContent, s.querySelector('strong').textContent]))""")
        caption("3 / 5", "Investigate",
                f"Read-only tools compare against the baseline: {stats.get('Findings')} findings, "
                f"{stats.get('Citations checked')} citations re-checked by code, {stats.get('Actions executed')} actions executed.")
        page.evaluate(OUTLINE_JS, "#analystContent .analytics-summary")
        hold(9)

        caption("4 / 5", "Explain", "Each finding ranks competing explanations by evidence, including benign and sensor-fault alternatives.")
        page.evaluate(OUTLINE_JS, "#analystContent .analyst-finding:nth-of-type(2)")
        hold(5)
        page.evaluate(OUTLINE_JS, "#analystContent .analyst-finding:nth-of-type(3)")
        hold(5)

        caption("5 / 5", "Evidence", "Findings link to the detections behind them.")
        chip = page.locator("#analystContent .analyst-finding:nth-of-type(1) .entity-chip").first
        key = chip.get_attribute("data-entity")
        chip.click()
        page.wait_for_selector(f"#identityTableWrap .entity-focus")
        hold(6, until=lambda: page.evaluate("() => Boolean(document.querySelector('#identityTableWrap .entity-focus'))"))

        page.goto(base.rstrip("/") + "/api/analyst/report", wait_until="domcontentloaded")
        caption("5 / 5", "Report", "Observed facts, computed results, and hypotheses are separated. Capture is proposed for approval, never executed.")
        page.evaluate("() => document.querySelector('h2:nth-of-type(3)')?.scrollIntoView({block: 'start'})")
        hold(7)
        browser.close()
    (frames / "timeline.json").write_text(json.dumps(timeline))
    print(json.dumps({"frames": len(timeline), "duration_s": timeline[-1] if timeline else 0, "focused_entity": key,
                      "stats": stats}))


if __name__ == "__main__":
    main()
