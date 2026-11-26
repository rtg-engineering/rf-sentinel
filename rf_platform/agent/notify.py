"""Deliver workflow alerts outside the dashboard: an operator-configured webhook. Off unless configured.

    RF_SENTINEL_ALERT_WEBHOOK=https://hooks.slack.com/services/...      # Slack / Mattermost / Teams-style {"text"}
    RF_SENTINEL_ALERT_WEBHOOK=https://ntfy.sh/<your-topic>               # ntfy: plain-text body, priority header
    RF_SENTINEL_ALERT_MIN_SEVERITY=warning                               # info | warning (default) | critical

The message carries the alert text, severity, device label, run id and whether the run is simulated. It never
carries IQ samples or evidence files. Delivery runs in the background with a short timeout and is recorded in
the run's timeline; a failed delivery never affects the run.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Callable
from urllib.parse import urlparse

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def webhook_url() -> str | None:
    url = os.getenv("RF_SENTINEL_ALERT_WEBHOOK", "").strip()
    return url if urlparse(url).scheme in ("http", "https") and urlparse(url).netloc else None


def describe() -> dict[str, Any]:
    """What the dashboard shows: whether alerts leave this machine, and to which host (never the full URL)."""
    url = webhook_url()
    return {"enabled": bool(url), "host": urlparse(url).netloc if url else None,
            "min_severity": os.getenv("RF_SENTINEL_ALERT_MIN_SEVERITY", "warning")}


def message(alert: dict[str, Any], run_id: str, simulated: bool) -> str:
    device = alert.get("device") or {}
    prefix = "[SIMULATED] " if simulated else ""
    return (f"{prefix}RF Sentinel {alert.get('severity', 'warning').upper()}: {alert.get('message')}"
            + (f" ({device.get('label')})" if device.get("label") and device.get("label") not in str(alert.get("message")) else "")
            + f" · run {run_id}")


def deliver(alert: dict[str, Any], run_id: str, simulated: bool,
            on_done: Callable[[str, str], None] | None = None, timeout_s: float = 5.0) -> threading.Thread | None:
    """Post the alert to the configured webhook in the background. Returns the thread, or None when disabled."""
    url = webhook_url()
    minimum = SEVERITY_RANK.get(os.getenv("RF_SENTINEL_ALERT_MIN_SEVERITY", "warning"), 1)
    if not url or SEVERITY_RANK.get(alert.get("severity", "warning"), 1) < minimum:
        return None
    text = message(alert, run_id, simulated)

    def post() -> None:
        import requests
        try:
            if "ntfy" in urlparse(url).netloc:
                response = requests.post(url, data=text.encode(), timeout=timeout_s,
                                         headers={"Title": "RF Sentinel alert", "Tags": "satellite_antenna",
                                                  "Priority": "high" if alert.get("severity") == "critical" else "default"})
            else:
                response = requests.post(url, json={"text": text}, timeout=timeout_s)
            ok = 200 <= response.status_code < 300
            status, detail = ("delivered", urlparse(url).netloc) if ok else ("failed", f"HTTP {response.status_code}")
        except Exception as exc:  # a delivery problem is reported, never raised into the run
            status, detail = "failed", f"{type(exc).__name__}"
        if on_done:
            on_done(status, detail)
    thread = threading.Thread(target=post, name="alert-webhook", daemon=True)
    thread.start()
    return thread
