from __future__ import annotations

import os
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _load_backend():
    app_path = Path(__file__).resolve().parents[1] / "ui" / "backend" / "app.py"
    backend_dir = str(app_path.parent)
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    spec = spec_from_file_location("rf_sentinel_ui_backend", app_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load UI backend: {app_path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    module = _load_backend()
    app = module.app
    from .sdr_backend import configure
    sdr = configure(bool(getattr(module, "RF_SENTINEL_DEMO_MODE", False)))
    print(f"[ui] SDR backend: {sdr.get('backend')}"
          + (f" via {sdr['gateway_url']}" if sdr.get("gateway_url") else "")
          + (f" ({', '.join(sdr['devices']) or 'no receivers found'})" if "devices" in sdr else "")
          + (f" unavailable: {sdr['error']}" if sdr.get("error") else ""), file=sys.stderr)
    command = getattr(module, "start_command_mode", None)
    if callable(command):
        try:
            started = command()
            if started:
                print(f"[ui] {started}", file=sys.stderr)
        except Exception as exc:  # a misconfigured link is reported; the local dashboard still starts
            print(f"[ui] Command link not started: {exc}", file=sys.stderr)
    resume = getattr(module, "resume_agent_watches", None)
    if callable(resume):
        try:
            for job in resume():
                print(f"[ui] {job['resumed']} ({job['job_id']})", file=sys.stderr)
        except Exception as exc:  # resuming is best effort; the dashboard starts regardless
            print(f"[ui] could not resume agent watches: {exc}", file=sys.stderr)
    host = os.getenv("RF_SENTINEL_HOST", os.getenv("BT_EXPLORER_HOST", "0.0.0.0"))
    port = int(os.getenv("RF_SENTINEL_PORT", os.getenv("BT_EXPLORER_PORT", "5050")))
    run_textual_console_dashboard = getattr(module, "run_textual_console_dashboard", None)
    if callable(run_textual_console_dashboard) and os.getenv("RF_SENTINEL_TEXTUAL_CONSOLE", "1").strip().lower() not in {"0", "false", "no", "off"}:
        def run_flask() -> None:
            try:
                app.run(host=host, port=port, threaded=True, use_reloader=False)
            except Exception as exc:
                append_log = getattr(module, "_console_append_log", None)
                if callable(append_log):
                    append_log(f"Flask server stopped: {exc}")

        flask_thread = module.threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        try:
            if run_textual_console_dashboard(host, port):
                return 0
        except KeyboardInterrupt:
            print("\n[ui] Ctrl+C received, stopping SDR streams...", file=sys.stderr)
        finally:
            shutdown = getattr(module, "shutdown", None)
            if callable(shutdown):
                shutdown()
        return 0
    start_console_dashboard = getattr(module, "start_console_dashboard", None)
    if callable(start_console_dashboard):
        start_console_dashboard(host, port)
    try:
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        print("\n[ui] Ctrl+C received, stopping SDR streams...", file=sys.stderr)
    finally:
        shutdown = getattr(module, "shutdown", None)
        if callable(shutdown):
            shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
