"""The few operations that make the station transmit, all on the host Bluetooth adapter (never the SDRs).

Each is bounded and runs only after the tool gateway has a named operator's approval for that exact operation
(gateway permission class "transmit": refused in observe mode, approval required in every other mode, passive-auto
included). Callers must call ``gateway.authorize`` first; these functions do not check.

- ``inquiry(seconds)``: a BR/EDR inquiry (bluetoothctl ``scan bredr on``). Discoverable devices answer, and their
  answers carry full addresses that the passive Classic decoder cannot get on its own.
- ``remote_name(address)``: a remote name request (``hcitool name``). It pages that one device and asks its name.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from typing import Any, Callable

MAX_INQUIRY_S = 30
BDADDR_RE = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")


def full_address(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text if BDADDR_RE.match(text) else None


def inquiry(seconds: float, popen: Callable[..., Any] = subprocess.Popen, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    seconds = max(1.0, min(float(seconds), MAX_INQUIRY_S))
    if popen is subprocess.Popen and shutil.which("bluetoothctl") is None:
        raise RuntimeError("bluetoothctl is not installed on this station")
    proc = popen(["bluetoothctl"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
    try:
        proc.stdin.write("power on\nscan bredr on\n")
        proc.stdin.flush()
        sleep(seconds)
        proc.stdin.write("scan off\nquit\n")
        proc.stdin.flush()
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    return {"seconds": seconds}


def remote_name(address: str, run: Callable[..., Any] = subprocess.run) -> str | None:
    addr = full_address(address)
    if addr is None:
        raise ValueError("a remote name request needs a full Bluetooth address (six octets)")
    if run is subprocess.run and shutil.which("hcitool") is None:
        raise RuntimeError("hcitool is not installed on this station")
    out = run(["hcitool", "name", addr], capture_output=True, text=True, timeout=15)
    name = (out.stdout or "").strip()
    return name or None
