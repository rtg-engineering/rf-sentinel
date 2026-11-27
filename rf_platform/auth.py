"""Operator authentication for the dashboard API: who is asking, and may they do this?

The agent's tool gateway decides whether an operation is *permitted* (mode, bounds, approvals). This module decides
whether the *caller* is a known operator with the role the request needs. Both checks apply.

Operators live in ``<log dir>/auth/operators.json``. Only a SHA-256 hash of each token is stored; the token itself
is shown once, when the operator is added (``rf-sentinel agent operators add NAME --role approver``).

Roles, each including the ones before it:
  viewer    read detections, runs, reports, plans
  operator  plan, run, stop, refine, save plans, ask, change scans and settings
  approver  also approve or deny an agent's pending operation

Modes (``RF_SENTINEL_AUTH``):
  required  every API call needs a known operator
  off       no authentication (a single-user bench behind a firewall)
  auto      (default) required when operators exist or the dashboard listens beyond this machine; off on loopback
            with no operators, and in demo mode (simulated data, no radio is used)
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
from typing import Any

ROLES = ("viewer", "operator", "approver")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
_lock = threading.Lock()


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def auth_dir(log_dir: Path) -> Path:
    return Path(os.getenv("RF_SENTINEL_AUTH_DIR") or Path(log_dir) / "auth")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def rank(role: str) -> int:
    return ROLES.index(role) if role in ROLES else -1


class OperatorStore:
    """operators.json: {"operators": {name: {"role", "token_sha256", "created_at"}}}. Read on every check, so an
    operator removed or demoted from the CLI loses access at once, including open browser sessions."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.path = self.directory / "operators.json"

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            return dict(json.loads(self.path.read_text()).get("operators") or {})
        except (OSError, ValueError):
            return {}

    def _save(self, operators: dict[str, dict[str, Any]]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"operators": operators}, indent=2, sort_keys=True) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def list(self) -> list[dict[str, Any]]:
        return [{"name": name, "role": o.get("role"), "created_at": o.get("created_at")}
                for name, o in sorted(self._load().items())]

    def add(self, name: str, role: str) -> str:
        """Create or re-key an operator; returns the new token (shown once, never stored)."""
        if not NAME_RE.match(name or ""):
            raise ValueError("operator names are 1-48 letters, digits, '.', '_' or '-'")
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        token = "rfs_" + secrets.token_urlsafe(32)
        with _lock:
            operators = self._load()
            operators[name] = {"role": role, "token_sha256": _hash(token),
                               "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self._save(operators)
        return token

    def remove(self, name: str) -> bool:
        with _lock:
            operators = self._load()
            if operators.pop(name, None) is None:
                return False
            self._save(operators)
        return True

    def identify(self, token: str) -> dict[str, str] | None:
        if not token:
            return None
        digest = _hash(token)
        for name, o in self._load().items():
            if hmac.compare_digest(str(o.get("token_sha256", "")), digest):
                return {"name": name, "role": str(o.get("role"))}
        return None

    def current(self, name: str) -> dict[str, str] | None:
        o = self._load().get(name)
        return {"name": name, "role": str(o.get("role"))} if o else None

    def any(self) -> bool:
        return bool(self._load())


def session_secret(directory: Path) -> bytes:
    """A per-installation key for signing browser sessions, created on first use."""
    path = Path(directory) / "session.key"
    with _lock:
        try:
            return bytes.fromhex(path.read_text().strip())
        except (OSError, ValueError):
            Path(directory).mkdir(parents=True, exist_ok=True)
            key = secrets.token_bytes(32)
            path.write_text(key.hex() + "\n")
            os.chmod(path, 0o600)
            return key


def _loopback(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")


def mode(store: OperatorStore, host: str, demo: bool) -> str:
    """'required' or 'off', with the reason, from RF_SENTINEL_AUTH and how the dashboard is exposed."""
    setting = (os.getenv("RF_SENTINEL_AUTH") or "auto").strip().lower()
    if setting in ("off", "0", "false", "no", "disabled"):
        return "off"
    if setting in ("required", "on", "1", "true", "yes"):
        return "required"
    if store.any():
        return "required"
    if demo or _loopback(host):
        return "off"
    return "required"


def needed_role(method: str, path: str) -> str | None:
    """The role an API request needs; None for paths that are always open (health, sign-in)."""
    if path in ("/api/health", "/api/auth/login", "/api/auth/logout", "/api/auth/me"):
        return None
    if re.fullmatch(r"/api/agent/jobs/[^/]+/(approve|deny)", path):
        return "approver"
    if method in ("GET", "HEAD", "OPTIONS"):
        return "viewer"
    # Plan, readiness, refine and ask change nothing on the radios, but they spend planner-model calls and reveal
    # receiver state, so they need an operator like every other POST.
    return "operator"


def bearer(header: str | None) -> str:
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""
