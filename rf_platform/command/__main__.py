"""Command setup helpers.

    python -m rf_platform.command pki init <dir> --broker-host <name-or-ip> [--broker-host ...]
    python -m rf_platform.command pki issue <dir> --station [--hostname H --arch A]
    python -m rf_platform.command pki issue <dir> --hub
    python -m rf_platform.command broker-config <dir> [--port 8883] [--bind ADDR] [--persistence DIR]
    python -m rf_platform.command identity
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import broker, pki
from .envelope import station_identity


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rf_platform.command", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pki")
    psub = p.add_subparsers(dest="pki_cmd", required=True)
    init = psub.add_parser("init")
    init.add_argument("dir", type=Path)
    init.add_argument("--broker-host", action="append", default=[])
    issue = psub.add_parser("issue")
    issue.add_argument("dir", type=Path)
    who = issue.add_mutually_exclusive_group(required=True)
    who.add_argument("--station", action="store_true")
    who.add_argument("--hub", action="store_true")
    issue.add_argument("--hostname")
    issue.add_argument("--arch")
    bc = sub.add_parser("broker-config")
    bc.add_argument("dir", type=Path)
    bc.add_argument("--port", type=int, default=8883)
    bc.add_argument("--bind")
    bc.add_argument("--persistence", type=Path)
    sub.add_parser("identity")
    args = parser.parse_args(argv)
    if args.cmd == "identity":
        print(json.dumps(station_identity()))
    elif args.cmd == "pki" and args.pki_cmd == "init":
        print(f"CA and broker certificate in {pki.init(args.dir, args.broker_host)}")
    elif args.cmd == "pki" and args.hub:
        print(f"hub certificate: {pki.issue_hub(args.dir)}")
    elif args.cmd == "pki":
        ident, cert = pki.issue_station(args.dir, args.hostname, args.arch)
        print(f"station {ident}: {cert}")
    else:
        print(f"broker config: {broker.write(args.dir, args.port, args.bind, args.persistence)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
