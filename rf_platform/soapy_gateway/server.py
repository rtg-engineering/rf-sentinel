"""HTTP + websocket service exposing SoapySDR receivers through the sdr-gateway API subset RF Sentinel uses.

  rf-sentinel-soapy-gateway --host 127.0.0.1 --port 8090

Routes: GET /health, GET /devices, GET /wifi/interfaces, streams (start, stop, retune, list,
WS /ws/iq/<id>), power sweeps (start, planned start, stop, list, samples), IQ sweeps (start, stop,
list, chunk, WS /ws/iq-sweeps/<id>). Errors use the gateway's {"detail": ...} shape. When
SDR_GATEWAY_API_TOKEN is set, requests need a Bearer token, X-API-Key, or ?token=.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import threading
import time
from typing import Any

from flask import Flask, jsonify, request

from .backend import DeviceBusy, SoapyBackend

STREAM_FIELDS = ("device_id", "center_freq_hz", "sample_rate_sps", "lna_gain_db", "vga_gain_db", "amp_enable",
                 "replace_existing", "baseband_filter_hz", "duration_seconds", "num_samples")
DEFAULT_CHUNK_BYTES = 262_144


def _stream_config(body: dict[str, Any], require_replace: bool = True) -> dict[str, Any]:
    config = {"lna_gain_db": 16, "vga_gain_db": 20, "amp_enable": False, "replace_existing": False,
              "baseband_filter_hz": None, "duration_seconds": None, "num_samples": None}
    config.update({k: body[k] for k in STREAM_FIELDS if k in body})
    for key in ("device_id", "center_freq_hz", "sample_rate_sps"):
        if key not in config:
            raise ValueError(f"{key} is required")
    return config


def _planned_sweep(backend: SoapyBackend, plan: dict[str, Any]) -> dict[str, Any]:
    """Same planning rules as sdr-gateway's /sweeps/plan/start."""
    device = backend.device(plan["device_id"])
    if plan.get("start_freq_hz") is not None or plan.get("stop_freq_hz") is not None:
        start, stop = int(plan["start_freq_hz"]), int(plan["stop_freq_hz"])
    elif plan.get("center_freq_hz") is not None:
        half = int(plan["span_hz"]) // 2
        start, stop = int(plan["center_freq_hz"]) - half, int(plan["center_freq_hz"]) + half
    elif plan.get("frequencies_hz"):
        margin = int(plan.get("margin_hz", 2_000_000))
        start, stop = min(plan["frequencies_hz"]) - margin, max(plan["frequencies_hz"]) + margin
    else:
        raise ValueError("provide start/stop, center/span, or frequencies_hz")
    start, stop = max(device.freq_min_hz, int(start)), min(device.freq_max_hz, int(stop))
    if start >= stop:
        raise ValueError("planned sweep start must be lower than stop after device range clipping")
    return {"device_id": device.id, "start_freq_hz": start, "stop_freq_hz": stop,
            "bin_width_hz": int(plan.get("bin_width_hz", 100_000)), "lna_gain_db": plan.get("lna_gain_db", 16),
            "vga_gain_db": plan.get("vga_gain_db", 20), "amp_enable": bool(plan.get("amp_enable"))}


_readers_lock = threading.Lock()


def reader_attached(session) -> None:
    with _readers_lock:
        session.extra["readers"] = session.extra.get("readers", 0) + 1


def reader_detached(session) -> None:
    with _readers_lock:
        session.extra["readers"] = max(0, session.extra.get("readers", 0) - 1)
        if not session.extra["readers"]:
            session.extra["idle_since"] = time.time()


def reap_idle_streams(backend: SoapyBackend, idle_s: float, now: float | None = None) -> list[str]:
    """Stop streams nobody has read for idle_s seconds. A client that dies without stopping its stream (killed by
    its parent, crashed) would otherwise hold the receiver until this service restarts."""
    now = time.time() if now is None else now
    stopped = []
    for session in backend.list("stream"):
        with _readers_lock:
            if session.extra.get("readers", 0):
                continue
            since = session.extra.setdefault("idle_since", now)  # never read yet: count from the first sweep
        if now - since >= idle_s:
            try:
                backend.stop(session.id)
                stopped.append(session.id)
            except KeyError:
                pass
    return stopped


class RedactToken:
    """Websocket clients pass the API token as ?token= (browsers cannot set headers there); keep it out of the log."""

    def filter(self, record) -> bool:
        import re
        message = record.getMessage()
        if "token=" in message:
            record.msg, record.args = re.sub(r"token=[^&\s\"]+", "token=***", message), ()
        return True


def create_app(backend: SoapyBackend, token: str | None = None) -> Flask:
    from flask_sock import Sock

    app = Flask("rf_sentinel_soapy_gateway")
    sock = Sock(app)
    expected = (token if token is not None else os.getenv("SDR_GATEWAY_API_TOKEN", "")).strip()

    def authorized() -> bool:
        if not expected:
            return True
        header = request.headers.get("Authorization", "")
        candidate = (header[7:].strip() if header.lower().startswith("bearer ") else "") \
            or request.headers.get("X-API-Key", "").strip() or request.args.get("token", "").strip()
        return bool(candidate) and hmac.compare_digest(candidate, expected)

    @app.before_request
    def _auth():
        if request.path != "/health" and not authorized():
            return jsonify({"detail": "Unauthorized"}), 401
        return None

    def fail(status: int, exc: Exception | str):
        return jsonify({"detail": str(exc).strip("'\"")}), status

    def body() -> dict[str, Any]:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def stream_state(s):
        state = {"stream_id": s.id, "status": s.status, "config": s.config}
        if s.worker:
            state["receiver"] = {"driver_path": s.worker.driver_path, "samples_received": s.worker.samples_received,
                                 "overflows": s.worker.overflows}
        if s.worker and s.worker.error:
            state["error"] = s.worker.error
        return state

    def sweep_state(s):
        return {"sweep_id": s.id, "status": s.status, "config": s.config}

    def iq_state(s):
        return {"iq_sweep_id": s.id, "stream_id": s.extra["stream_id"], "status": s.status, "config": s.config,
                "current_center_freq_hz": s.worker.center_hz, "point_index": s.extra["point_index"]}

    def guarded(fn):
        try:
            return fn()
        except KeyError as exc:
            return fail(404, exc)
        except DeviceBusy as exc:
            return fail(409, exc)
        except (ValueError, TypeError) as exc:
            return fail(400, exc)

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "service": "rf-sentinel-soapy-gateway", "backend": "soapysdr"})

    @app.get("/devices")
    def devices():
        if request.args.get("refresh") in ("1", "true"):
            backend.refresh_devices()
        return jsonify(backend.devices())

    @app.get("/wifi/interfaces")
    def wifi_interfaces():
        return jsonify([])  # WiFi monitor-mode adapters are not SoapySDR devices

    @app.get("/streams")
    def list_streams():
        return jsonify([stream_state(s) for s in backend.list("stream")])

    @app.post("/streams/start")
    def start_stream():
        return guarded(lambda: jsonify(stream_state(backend.start_stream(_stream_config(body())))))

    @app.post("/streams/<stream_id>/stop")
    def stop_stream(stream_id):
        return guarded(lambda: (backend.stop(backend.get(stream_id, "stream").id), jsonify({"ok": True}))[1])

    @app.post("/streams/<stream_id>/retune")
    def retune_stream(stream_id):
        return guarded(lambda: jsonify(stream_state(backend.retune_stream(stream_id, body()))))

    @sock.route("/ws/iq/<stream_id>")
    def ws_iq(ws, stream_id):
        try:
            session = backend.get(stream_id, "stream")
        except KeyError:
            ws.close(reason=1008, message=f"Unknown stream_id {stream_id}")
            return
        keep = request.args.get("keep", "").lower() in ("1", "true", "yes")
        cursor = session.worker.cursor(request.args.get("start", "latest"))
        reader_attached(session)
        try:
            while True:
                cursor, chunk = session.worker.read(cursor, timeout_s=1.0)
                if chunk:
                    ws.send(chunk)
                elif not session.worker.alive or session.extra.get("stopped"):
                    break
        except Exception:
            pass
        finally:
            reader_detached(session)
            if not keep:
                try:
                    backend.stop(stream_id)
                except KeyError:
                    pass

    @app.get("/sweeps")
    def list_sweeps():
        return jsonify([sweep_state(s) for s in backend.list("sweep")])

    @app.post("/sweeps/start")
    def start_sweep():
        return guarded(lambda: jsonify(sweep_state(backend.start_sweep(body()))))

    @app.post("/sweeps/plan/start")
    def start_planned_sweep():
        def run():
            plan = body()
            session = backend.start_sweep(_planned_sweep(backend, plan))
            return jsonify({**sweep_state(session), "engine": session.extra["engine"], "label": plan.get("label")})
        return guarded(run)

    @app.post("/sweeps/<sweep_id>/stop")
    def stop_sweep(sweep_id):
        return guarded(lambda: (backend.stop(backend.get(sweep_id, "sweep").id), jsonify({"ok": True}))[1])

    @app.get("/sweeps/<sweep_id>/samples")
    def sweep_samples(sweep_id):
        return guarded(lambda: jsonify(list(backend.get(sweep_id, "sweep").extra["samples"])))

    @app.get("/iq-sweeps")
    def list_iq_sweeps():
        return jsonify([iq_state(s) for s in backend.list("iq_sweep")])

    @app.post("/iq-sweeps/start")
    def start_iq_sweep():
        return guarded(lambda: jsonify(iq_state(backend.start_iq_sweep(body()))))

    @app.post("/iq-sweeps/<sweep_id>/stop")
    def stop_iq_sweep(sweep_id):
        return guarded(lambda: (backend.stop(backend.get(sweep_id, "iq_sweep").id), jsonify({"ok": True}))[1])

    @app.get("/iq-sweeps/<sweep_id>/chunk")
    def iq_sweep_chunk(sweep_id):
        nbytes = max(1024, min(int(request.args.get("nbytes", DEFAULT_CHUNK_BYTES)), 4 * 1024 * 1024))
        return guarded(lambda: jsonify(backend.iq_sweep_chunk(sweep_id, nbytes)))

    @sock.route("/ws/iq-sweeps/<sweep_id>")
    def ws_iq_sweep(ws, sweep_id):
        try:
            backend.get(sweep_id, "iq_sweep")
        except KeyError:
            ws.close(reason=1008, message=f"Unknown iq_sweep_id {sweep_id}")
            return
        nbytes = max(1024, min(int(request.args.get("nbytes", DEFAULT_CHUNK_BYTES)), 4 * 1024 * 1024))
        keep = request.args.get("keep", "").lower() in ("1", "true", "yes")
        try:
            while True:
                payload = backend.iq_sweep_chunk(sweep_id, nbytes)
                if not payload["byte_count"]:
                    break
                ws.send(json.dumps(payload))
        except Exception:
            pass
        finally:
            if not keep:
                try:
                    backend.stop(sweep_id)
                except KeyError:
                    pass

    return app


def serve_in_thread(host: str = "127.0.0.1", port: int = 8090, backend: SoapyBackend | None = None):
    """Start the service on a background thread (used by the dashboard when SDR_BACKEND=soapy)."""
    from werkzeug.serving import make_server
    backend = backend or SoapyBackend()
    server = make_server(host, port, create_app(backend), threaded=True)
    thread = threading.Thread(target=server.serve_forever, name="soapy-gateway", daemon=True)
    thread.start()
    return server, backend


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=os.getenv("RF_SENTINEL_SOAPY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("RF_SENTINEL_SOAPY_PORT", "8090")))
    parser.add_argument("--include-non-rf", action="store_true", help="Also list non-RF SoapySDR devices (e.g. audio)")
    args = parser.parse_args(argv)
    from werkzeug.serving import make_server
    import faulthandler
    import logging
    # A crash in native code (libbladeRF / SoapySDR via ctypes) kills the process without a Python traceback;
    # this prints every thread's stack to stderr on SIGSEGV/SIGABRT/SIGBUS so the log says where it happened.
    faulthandler.enable(all_threads=True)
    logging.getLogger("werkzeug").addFilter(RedactToken())
    backend = SoapyBackend(include_non_rf=args.include_non_rf)
    print(f"rf-sentinel-soapy-gateway on http://{args.host}:{args.port} "
          f"({len(backend.refresh_devices())} SoapySDR receiver(s))", flush=True)
    server = make_server(args.host, args.port, create_app(backend), threaded=True)
    idle_s = float(os.getenv("RF_SENTINEL_STREAM_IDLE_S", "30"))

    def reaper():
        while True:
            time.sleep(5)
            for sid in reap_idle_streams(backend, idle_s):
                print(f"stopped {sid}: no reader for {idle_s:.0f} s", flush=True)
    if idle_s > 0:
        threading.Thread(target=reaper, name="idle-stream-reaper", daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        backend.stop_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
