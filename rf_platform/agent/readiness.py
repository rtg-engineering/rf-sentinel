"""Readiness: can this plan run here, now? Checked before Run so a watch left overnight does not fail at cycle 1.

Each check is a quick, read-only probe (no receiver is opened, nothing is collected): the IQ service and its
receivers for SDR protocols, the cellular survey service, Bluetooth Classic support, free disk space for evidence,
and where alerts will go. Result: {"ok", "checks": [{"name", "status": ok|warn|fail, "detail"}]}.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .playbook import PlaybookSpec
from .profiles import PROFILES
from .workflow import WorkflowSpec

MIN_FREE_BYTES = 2 * 1024 ** 3
FULL_SCAN_S = 75  # the modem's full scan took ~56 s live, plus the first quick poll and margin


def _plan_parts(plan: dict[str, Any]) -> tuple[list[WorkflowSpec], PlaybookSpec | None]:
    if plan.get("playbook"):
        book = PlaybookSpec.model_validate(plan["playbook"])
        return [s.workflow for s in book.steps], book
    return [WorkflowSpec.model_validate(plan["spec"])], None


def check(plan: dict[str, Any], radio, mode: str, log_dir: Path, simulate: bool = False,
          cellular=None, classic=None) -> dict[str, Any]:
    """radio: an agent Radio/SimRadio. cellular/classic: optional capability callables (for tests)."""
    if plan.get("discovery"):
        return _check_discovery(radio, log_dir, simulate)
    specs, book = _plan_parts(plan)
    protocols = sorted({p for s in specs for p in s.collect})
    sdr = [p for p in protocols if p != "cellular"]
    checks: list[dict[str, Any]] = []
    add = lambda name, status, detail: checks.append({"name": name, "status": status, "detail": detail})  # noqa: E731
    if simulate:
        add("Receivers", "ok", "Simulated receivers (no radio is used)")
    elif sdr:
        try:
            devices = radio.devices()
        except Exception as exc:
            devices = None
            add("IQ service", "fail", str(exc))
        if devices is not None:
            add("IQ service", "ok", f"{len(devices)} receiver(s): " + (", ".join(d["id"] for d in devices) or "none"))
            for name in sdr:
                profile = PROFILES[name]
                fits = [d for d in devices if d.get("freq_min_hz", 0) <= min(profile.freqs_hz or (0,))
                        and max(profile.freqs_hz or (0,)) <= d.get("freq_max_hz", 0)
                        and d.get("max_sample_rate_sps", 0) >= profile.sample_rate_sps]
                if not fits:
                    add(profile.title, "fail", "no attached receiver covers this band at the needed sample rate")
                elif all(d.get("occupied") for d in fits):
                    add(profile.title, "warn", "every suitable receiver is busy right now: " + ", ".join(d["id"] for d in fits))
                else:
                    add(profile.title, "ok", ", ".join(d["id"] for d in fits if not d.get("occupied")))
            if "classic" in sdr:
                from .capabilities import classic_capability
                from .core import plugin_path
                cap = (classic or (lambda: classic_capability(devices, plugin_path(PROFILES["classic"].plugin))))()
                add("Bluetooth Classic decoder", "ok" if cap["supported"] else "warn", cap["reason"])
    if "cellular" in protocols and not simulate:
        from rf_platform.cellular.survey import cellular_capability
        cap = (cellular or cellular_capability)()
        add("Cellular modem", "ok" if cap["supported"] else "fail", cap["reason"])
    short = [s.duration_s for s in specs if "cellular" in s.collect and s.duration_s < FULL_SCAN_S]
    if short and not simulate:
        # Measured on the RM520N-GL: the full scan (AT+QSCAN) holds the modem for about a minute. A shorter survey
        # still lists serving and neighbour cells, but neighbours then have no operator (PLMN), so operator rules
        # cannot judge them.
        add("Cell survey length", "warn",
            f"{min(short):.0f} s is shorter than the full network scan (about a minute): neighbour cells will be "
            "listed without an operator; survey for 90 s or more to name them")
    try:
        free = shutil.disk_usage(log_dir if log_dir.exists() else log_dir.parent).free
        records_iq = any(a.kind == "record_iq" for s in specs for t in s.triggers for a in t.do)
        add("Disk space", "ok" if free >= MIN_FREE_BYTES else ("fail" if records_iq else "warn"),
            f"{free / 1024 ** 3:.1f} GB free for reports" + (" and IQ evidence" if records_iq else ""))
    except OSError as exc:
        add("Disk space", "warn", f"could not check: {exc}")
    long_run = book is not None and (book.repeat or book.start)
    if long_run and mode == "approval-required":
        add("Unattended", "warn", "Approve mode: every cycle waits for a named operator; an unanswered approval expires")
    from . import notify
    alerting = any(a.kind == "alert" for s in specs for t in s.triggers for a in t.do)
    if alerting:
        d = notify.describe()
        add("Alert delivery", "ok" if d["enabled"] else ("warn" if long_run else "ok"),
            f"alerts also go to {d['host']} ({d['min_severity']} and above)" if d["enabled"] else
            "dashboard only (set RF_SENTINEL_ALERT_WEBHOOK to get alerts away from this screen)")
    worst = "fail" if any(c["status"] == "fail" for c in checks) else "warn" if any(c["status"] == "warn" for c in checks) else "ok"
    return {"ok": worst != "fail", "status": worst, "checks": checks}


def _check_discovery(radio, log_dir: Path, simulate: bool) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    add = lambda name, status, detail: checks.append({"name": name, "status": status, "detail": detail})  # noqa: E731
    if simulate:
        add("Receivers", "ok", "Simulated receivers (no radio is used)")
    else:
        try:
            devices = radio.devices()
            wide = [d for d in devices if d.get("max_sample_rate_sps", 0) >= 20_000_000]
            free = [d for d in wide if not d.get("occupied")]
            add("IQ service", "ok", f"{len(devices)} receiver(s)")
            if not wide:
                add("Wideband receiver", "fail", "a sweep needs a receiver with at least 20 MS/s")
            else:
                span = max(wide, key=lambda d: d.get("freq_max_hz", 0) - d.get("freq_min_hz", 0))
                add("Sweep range", "ok" if free else "warn",
                    f"{span['freq_min_hz'] / 1e6:.0f} MHz–{span['freq_max_hz'] / 1e9:.1f} GHz on "
                    + (", ".join(d["id"] for d in free) if free else "busy receivers only"))
        except Exception as exc:
            add("IQ service", "fail", str(exc))
    try:
        add("Disk space", "ok", f"{shutil.disk_usage(log_dir if log_dir.exists() else log_dir.parent).free / 1024 ** 3:.1f} GB free")
    except OSError as exc:
        add("Disk space", "warn", f"could not check: {exc}")
    worst = "fail" if any(c["status"] == "fail" for c in checks) else "warn" if any(c["status"] == "warn" for c in checks) else "ok"
    return {"ok": worst != "fail", "status": worst, "checks": checks}
