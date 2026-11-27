"""A small private CA for one Command site: the broker, the hub, and each station get a certificate.

    python -m rf_platform.command pki init <dir> --broker-host <name-or-ip> [...]
    python -m rf_platform.command pki issue <dir> --station            # this machine: CN = <hostname>/<arch>
    python -m rf_platform.command pki issue <dir> --hub

A station's certificate common name is its identity (``<hostname>/<arch>``). The broker uses it as the MQTT username
(``use_identity_as_username``), so the ACL can confine each station to its own topics. Uses the ``openssl``
command (no Python crypto dependency). Keys are written 0600. Keep ``ca.key`` off the stations.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import shutil
import subprocess

from .envelope import station_identity
from .topics import HUB_IDENTITY

DAYS = 825


def _openssl(*args: str, cwd: Path) -> None:
    if shutil.which("openssl") is None:
        raise RuntimeError("the openssl command is required to issue certificates")
    out = subprocess.run(["openssl", *args], cwd=cwd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"openssl {args[0]} failed: {(out.stderr or out.stdout).strip()[-400:]}")


def _private(path: Path) -> None:
    os.chmod(path, 0o600)


def init(directory: Path, broker_hosts: list[str]) -> Path:
    """Create the CA and the broker's server certificate (with the names clients will connect to)."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    if (d / "ca.key").exists():
        raise FileExistsError(f"{d} already has a CA")
    _openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
             "-keyout", "ca.key", "-out", "ca.crt", "-days", str(DAYS * 2), "-subj", "/CN=RF Sentinel Command CA", cwd=d)
    _private(d / "ca.key")
    sans = []
    for host in broker_hosts or ["localhost"]:
        try:
            ipaddress.ip_address(host)
            sans.append(f"IP:{host}")
        except ValueError:
            sans.append(f"DNS:{host}")
    _issue(d, "broker", "rfs-broker", ",".join(sans), server=True)
    return d


def _issue(d: Path, name: str, common_name: str, san: str | None = None, server: bool = False) -> Path:
    if not (d / "ca.key").exists():
        raise FileNotFoundError(f"{d} has no CA (run: pki init)")
    ext = d / f"{name}.ext"
    ext.write_text("basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n"
                   f"extendedKeyUsage={'serverAuth' if server else 'clientAuth'}\n"
                   + (f"subjectAltName={san}\n" if san else ""))
    _openssl("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", f"{name}.key",
             "-out", f"{name}.csr", "-subj", "/CN=" + common_name.replace("/", "\\/"), cwd=d)  # "/" is openssl's RDN separator
    _private(d / f"{name}.key")
    _openssl("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial",
             "-out", f"{name}.crt", "-days", str(DAYS), "-extfile", ext.name, cwd=d)
    (d / f"{name}.csr").unlink(missing_ok=True)
    ext.unlink(missing_ok=True)
    return d / f"{name}.crt"


def issue_station(directory: Path, hostname: str | None = None, arch: str | None = None) -> tuple[str, Path]:
    ident = station_identity(hostname, arch)
    name = f"station-{ident['hostname']}-{ident['arch']}"
    return ident["id"], _issue(Path(directory), name, ident["id"])


def issue_hub(directory: Path) -> Path:
    return _issue(Path(directory), "hub", HUB_IDENTITY)


def common_name(cert: Path) -> str:
    out = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-subject", "-nameopt", "RFC2253"],
                         check=True, capture_output=True, text=True).stdout.strip()
    for part in out.removeprefix("subject=").split(","):
        if part.strip().startswith("CN="):
            return part.strip()[3:]
    raise ValueError(f"{cert} has no common name")
