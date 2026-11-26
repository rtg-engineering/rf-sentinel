"""What this installation can actually collect, checked before the agent promises it."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
from typing import Any

CLASSIC_RATE_SPS = 20_000_000          # 20 one-megahertz Bluetooth channels per capture
CLASSIC_BAND_HZ = (2_402_000_000, 2_480_000_000)
CLASSIC_PLUGIN = "bluetooth_classic"
CLASSIC_DECODER = "btcexplorer-sniffer-gateway"


def classic_decoder_binary() -> Path | None:
    """The native Classic decoder that reads IQ from the IQ service (built from the plugin source)."""
    spec = importlib.util.find_spec("bluetooth_classic")
    if spec is None or not spec.origin:
        return None
    root = Path(spec.origin).resolve().parents[2]
    for build in ("build", "build-user"):
        candidate = root / build / CLASSIC_DECODER
        if candidate.exists():
            return candidate
    return None


def classic_capability(devices: list[dict[str, Any]], plugin: str | None, exclude: tuple[str, ...] = ()) -> dict[str, Any]:
    """Whether Bluetooth Classic discovery can run now, on which receiver, and if not, why not.

    Needs the bluetooth-classic plugin, its native decoder (or cmake to build it on first use), and a free
    receiver covering 2.4 GHz at 20 MS/s or more. Receive-only: the plugin's transmitting page-stimulus
    mode is never used by the agent.
    """
    if not plugin:
        return {"supported": False, "reason": "the bluetooth-classic plugin is not installed "
                                              "(pip install -e rf_platform/plugins/bluetooth-classic)"}
    binary = classic_decoder_binary()
    if binary is None and not shutil.which("cmake"):
        return {"supported": False, "reason": "the native Classic decoder is not built and cmake is not available to build it"}
    covers = [d for d in devices if d.get("freq_min_hz", 0) <= CLASSIC_BAND_HZ[0]
              and CLASSIC_BAND_HZ[1] <= d.get("freq_max_hz", 0)]
    wide = [d for d in covers if d.get("max_sample_rate_sps", 0) >= CLASSIC_RATE_SPS]
    if not wide:
        names = ", ".join(f"{d['id']} (max {d.get('max_sample_rate_sps', 0) / 1e6:.0f} MS/s)" for d in covers) or "none at 2.4 GHz"
        return {"supported": False, "reason": f"Classic needs a 2.4 GHz receiver that samples at least "
                                              f"{CLASSIC_RATE_SPS / 1e6:.0f} MS/s; attached: {names}"}
    free = [d for d in wide if not d.get("occupied") and d["id"] not in exclude]
    if not free:
        busy = ", ".join(d["id"] for d in wide)
        return {"supported": False, "reason": f"no free wideband receiver for Classic ({busy} in use)"
                                              + (" - BLE is using one; attach a second receiver to run both" if exclude else "")}
    return {"supported": True, "device": free[0],
            "decoder": str(binary) if binary else "built from source on first use",
            "reason": f"{free[0]['id']} at {CLASSIC_RATE_SPS / 1e6:.0f} MS/s"}
