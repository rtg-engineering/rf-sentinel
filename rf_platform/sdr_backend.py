"""Choose where RF Sentinel gets IQ samples. SoapySDR is the default.

  SDR_BACKEND=soapy    (default) start the built-in SoapySDR IQ service on 127.0.0.1:RF_SENTINEL_SOAPY_PORT
                       (8790) and point SDR_GATEWAY_BASE_URL at it, so every receiver plugin uses it
  SDR_BACKEND=gateway  use an external sdr-gateway at SDR_GATEWAY_BASE_URL
  SDR_BACKEND=rfiq     BT/BLE combined scanner reads rfiq_daemon sockets (station deployments)

An explicitly set SDR_GATEWAY_BASE_URL always wins over the built-in service. Demo mode uses no radios.
If SoapySDR cannot be loaded, the dashboard still starts and the reason is reported.
"""
from __future__ import annotations

import atexit
import os
from typing import Any

DEFAULT_SOAPY_PORT = 8790
_STATE: dict[str, Any] = {"backend": "unconfigured"}


def configure(demo_mode: bool = False) -> dict[str, Any]:
    global _STATE
    requested = os.getenv("SDR_BACKEND", "soapy").strip().lower() or "soapy"
    if demo_mode:
        _STATE = {"backend": "demo", "detail": "Synthetic replay; no radios are opened."}
    elif requested != "soapy":
        _STATE = {"backend": requested, "gateway_url": os.getenv("SDR_GATEWAY_BASE_URL", "http://127.0.0.1:8080")}
    elif os.getenv("SDR_GATEWAY_BASE_URL"):
        _STATE = {"backend": "gateway", "gateway_url": os.environ["SDR_GATEWAY_BASE_URL"],
                  "detail": "SDR_GATEWAY_BASE_URL is set explicitly; using that gateway instead of the built-in service."}
    else:
        host = "127.0.0.1"
        port = int(os.getenv("RF_SENTINEL_SOAPY_PORT", str(DEFAULT_SOAPY_PORT)))
        try:
            from .soapy_gateway.server import serve_in_thread
            server, backend = serve_in_thread(host, port)
            devices = backend.refresh_devices()
        except Exception as exc:  # SoapySDR missing, or the port is taken
            _STATE = {"backend": "soapy", "available": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
            return dict(_STATE)
        url = f"http://{host}:{port}"
        os.environ["SDR_GATEWAY_BASE_URL"] = url  # inherited by scanner subprocesses and plugins
        atexit.register(backend.stop_all)
        atexit.register(server.shutdown)
        _STATE = {"backend": "soapy", "available": True, "gateway_url": url,
                  "devices": [d.id for d in devices]}
    return dict(_STATE)


def state() -> dict[str, Any]:
    return dict(_STATE)
