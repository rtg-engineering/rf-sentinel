"""Run an external detector script with RF Sentinel's IQ service as its receiver.

    python -m drone_rf.shim /path/to/detector.py [detector args...]

Loads the detector as a module and wraps its source_class_for_device(device_args) so "driver=rfsentinel" devices
get RfSentinelIqSource; every other device keeps the detector's own source. SIGTERM becomes KeyboardInterrupt, which
the detector handles by stopping its scanners and closing their sources (so the IQ streams are released).
"""
from __future__ import annotations

import importlib.util
import signal
import sys
from pathlib import Path

from .source import RfSentinelIqSource, is_rfsentinel

HOOK = "source_class_for_device"


def load(path: str):
    path = str(Path(path).resolve())
    spec = importlib.util.spec_from_file_location("drone_rf_external_detector", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load detector {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses and pickling look modules up by name
    spec.loader.exec_module(module)
    return module


def install(module) -> None:
    original = getattr(module, HOOK, None)
    if original is None:
        raise SystemExit(f"detector has no {HOOK}(device_args): it cannot take an external receiver")

    def choose(device_args):
        return RfSentinelIqSource if is_rfsentinel(device_args) else original(device_args)

    setattr(module, HOOK, choose)


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit("usage: python -m drone_rf.shim DETECTOR.py [args...]")
    module = load(argv[0])
    install(module)
    signal.signal(signal.SIGTERM, _interrupt)
    sys.argv = [argv[0], *argv[1:]]
    return int(module.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
