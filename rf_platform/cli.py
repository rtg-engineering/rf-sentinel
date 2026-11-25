"""`rf-sentinel` command: rf-sentinel agent ... | rf-sentinel analyst ..."""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: rf-sentinel {agent,analyst} ...\n\n"
              "  agent    plain-language, receive-only collection (run, status, stop, list, approve, deny, report)\n"
              "  analyst  read-only investigations, memory, and missions")
        return 0
    if argv[0] == "agent":
        from rf_platform.agent.__main__ import main as agent_main
        return agent_main(argv[1:])
    if argv[0] == "analyst":
        from rf_platform.analyst.__main__ import main as analyst_main
        return analyst_main(argv[1:])
    print(f"unknown command {argv[0]!r}; try: rf-sentinel agent --help", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
