"""Client and normalisation for a cellular survey service (rm520n-survey) that owns a Quectel modem.

The modem reports cells it can hear: serving and neighbour cells (AT+QENG, every few seconds) and a full
network scan (AT+QSCAN, about a minute). Without a SIM the modem does not register on any network, so the
survey only listens. With a SIM inserted it registers like any phone, which does transmit.
"""
from __future__ import annotations

import os
from typing import Any

import requests

DEFAULT_URL = "http://127.0.0.1:5098"

# Common US PLMNs (MCC-MNC). Anything not listed is reported as "unknown operator", not guessed.
OPERATORS = {
    "310410": "AT&T", "310280": "AT&T", "310150": "AT&T", "310170": "AT&T", "310380": "AT&T", "310560": "AT&T",
    "313100": "AT&T FirstNet", "313110": "AT&T FirstNet",
    "311480": "Verizon", "310004": "Verizon", "310012": "Verizon", "311270": "Verizon", "310590": "Verizon",
    "310260": "T-Mobile", "310240": "T-Mobile", "310660": "T-Mobile", "310160": "T-Mobile", "310200": "T-Mobile",
    "310210": "T-Mobile", "310220": "T-Mobile", "310230": "T-Mobile", "310250": "T-Mobile", "310270": "T-Mobile",
    "310310": "T-Mobile", "310490": "T-Mobile", "310580": "T-Mobile", "310800": "T-Mobile", "311490": "T-Mobile",
    "311660": "T-Mobile", "312250": "T-Mobile", "313340": "Dish", "313790": "Dish", "311580": "US Cellular",
    "310120": "Sprint (T-Mobile)", "312530": "Sprint (T-Mobile)",
}
RAT_LABELS = {"NR5G": "5G NR", "NR5G-SA": "5G NR", "NR5G-NSA": "5G NR", "LTE": "LTE", "WCDMA": "3G WCDMA",
              "GSM": "2G GSM", "CDMA": "2G CDMA", "EVDO": "3G EVDO", "TDSCDMA": "3G TD-SCDMA"}
GENERATION = {"5G NR": "5G", "LTE": "4G", "3G WCDMA": "3G", "3G EVDO": "3G", "3G TD-SCDMA": "3G",
              "2G GSM": "2G", "2G CDMA": "2G"}


def survey_url() -> str:
    return (os.getenv("RF_SENTINEL_CELL_SURVEY_URL") or DEFAULT_URL).rstrip("/")


class SurveyClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or survey_url()).rstrip("/")

    def _post(self, path: str, timeout: float) -> dict[str, Any]:
        response = requests.post(f"{self.base_url}{path}", json={}, timeout=timeout)
        data = response.json()
        if response.status_code >= 400 or not data.get("ok", False):
            raise RuntimeError(data.get("error") or f"{path} failed ({response.status_code})")
        return data

    def health(self, timeout: float = 4) -> dict[str, Any]:
        return requests.get(f"{self.base_url}/api/health", timeout=timeout).json()

    def ports(self, timeout: float = 15) -> dict[str, Any]:
        return requests.get(f"{self.base_url}/api/ports", timeout=timeout).json()

    def scan(self) -> list[dict[str, Any]]:
        """Serving + neighbour cells (fast)."""
        data = self._post("/api/scan", 30)
        return list(data.get("serving") or []) + list(data.get("neighbors") or [])

    def qscan(self) -> list[dict[str, Any]]:
        """Full network scan (slow, ~1 min)."""
        return list(self._post("/api/qscan", 240).get("scanned") or [])


def cellular_capability(base_url: str | None = None) -> dict[str, Any]:
    """Whether a cellular survey modem is reachable through the survey service, and on which port."""
    client = SurveyClient(base_url)
    try:
        health = client.health()
    except Exception as exc:
        return {"supported": False, "reason": f"no cellular survey service at {client.base_url} "
                                              f"({type(exc).__name__}); start rm520n-survey or set RF_SENTINEL_CELL_SURVEY_URL"}
    try:
        ports = client.ports()
    except Exception:
        ports = {}
    if not any(p.get("ok") for p in ports.get("ports", [])) and not ports.get("active_port"):
        return {"supported": False, "reason": f"the survey service at {client.base_url} sees no modem answering AT commands"}
    return {"supported": True, "url": client.base_url, "unit": health.get("unit"),
            "port": ports.get("active_port"),
            "reason": f"cellular modem via {client.base_url} (port {ports.get('active_port') or 'auto'}). Receive-only "
                      "with no SIM; with a SIM inserted the modem registers on a network, which transmits."}


def normalise(observation: dict[str, Any], known_plmns: dict[tuple[str, str], str] | None = None) -> dict[str, Any] | None:
    """Survey observation -> RF Sentinel detection event (kind "cell")."""
    identity = observation.get("identity") or {}
    signal = observation.get("signal") or {}
    rat = RAT_LABELS.get(str(identity.get("rat") or "").upper(), str(identity.get("rat") or "Cellular"))
    cell_id = str(identity.get("cell_id") or "").upper()
    plmn = str(identity.get("plmn") or "")
    verified = observation.get("role") == "scanned"
    if known_plmns is not None and cell_id:
        if observation.get("role") == "scanned" and plmn:
            known_plmns.setdefault((rat, cell_id), plmn)  # a shared cell lists several PLMNs; keep the first
        elif (rat, cell_id) in known_plmns:
            # The serving-cell report can carry a mis-parsed PLMN; the full scan of the same cell wins.
            plmn, verified = known_plmns[(rat, cell_id)], True
    if not plmn:
        operator = "PLMN not reported"
    elif verified:
        operator = OPERATORS.get(plmn, "unknown operator")
    else:
        operator = OPERATORS.get(plmn) or f"unverified PLMN {plmn[:3]}-{plmn[3:]}"
    pci = identity.get("pci")
    arfcn = identity.get("nr_arfcn") or identity.get("earfcn")
    if not cell_id and pci is None:
        return None
    # Neighbour reports carry only PCI and channel; the serving report and the full scan also carry the cell ID.
    # (type, PCI, channel) is one physical cell, so once its cell ID is known both reports share one key, and the
    # first named report says which PCI-only key it replaces so earlier sightings can be folded in.
    pci_key = f"{rat}:PCI{pci}@{arfcn}" if pci is not None and arfcn is not None else None
    supersedes = None
    if known_plmns is not None and pci_key:
        alias = ("cell", rat, pci, arfcn)
        if cell_id:
            if alias not in known_plmns:
                known_plmns[alias] = cell_id
                supersedes = pci_key
        elif alias in known_plmns:
            cell_id = known_plmns[alias]
            if not plmn and (rat, cell_id) in known_plmns:
                plmn, verified = known_plmns[(rat, cell_id)], True
                operator = OPERATORS.get(plmn, "unknown operator")
    key = f"{rat}:{cell_id or 'PCI' + str(pci) + '@' + str(arfcn)}"  # one physical cell, whatever PLMNs it lists
    rsrp = signal.get("rsrp_dbm")
    role = str(observation.get("role") or "scanned")
    return {
        "kind": "cell", "protocol": "cellular", "key": key, "role": role, "rat": rat,
        "generation": GENERATION.get(rat, ""), "plmn": plmn, "mcc": identity.get("mcc"), "mnc": identity.get("mnc"),
        "operator": operator, "plmn_verified": verified, "cell_id": cell_id, "pci": pci, "arfcn": arfcn,
        "tac": identity.get("tac"), "band": identity.get("band"),
        "rsrp_dbm": rsrp, "rsrq_db": signal.get("rsrq_db"), "sinr_db": signal.get("sinr_db"),
        "rssi_dbfs": rsrp,  # generic signal field used by filters; for cells it is RSRP in dBm
        "identity": f"{operator} · {rat}" + (f" · PCI {pci}" if pci is not None else ""),
        "source": observation.get("source"), "observed_at": observation.get("timestamp"),
    } | ({"supersedes": supersedes} if supersedes else {})
