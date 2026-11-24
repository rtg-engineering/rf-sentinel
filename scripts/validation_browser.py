#!/usr/bin/env python3
"""Use the shipped UI's analytics functions and render the JSON-derived card."""
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1200, "height": 900}, device_scale_factor=1)
    if sys.argv[1] == "analytics":
        # Evaluate the actual frontend functions against captured normal API rows.
        page.goto(sys.argv[2], wait_until="domcontentloaded")
        rows = json.loads(Path(sys.argv[3]).read_text())
        result = page.evaluate('''rows => ({
            recurring_pattern_count: rows.filter(r => patternScore(r) > 1).length,
            weekend_pattern_count: rows.filter(r => patternScore(r) > 1 && analyticsForRow(r).weekend).length,
            anomaly_count: rows.filter(r => analyticsForRow(r).activeAnomalies.length).length,
            patterns: rows.map(r => ({key:r.key, weekly:analyticsForRow(r).weekly,
                weekend:analyticsForRow(r).weekend, span:r.seen_span_days,
                anomalies:analyticsForRow(r).activeAnomalies})).sort((a,b)=>a.key.localeCompare(b.key))
        })''', rows)
        print(json.dumps(result))
    else:
        page.goto(Path(sys.argv[2]).resolve().as_uri())
        page.locator("main").screenshot(path=sys.argv[3])
    browser.close()
