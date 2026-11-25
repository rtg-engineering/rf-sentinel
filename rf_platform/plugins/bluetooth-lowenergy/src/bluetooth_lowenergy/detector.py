from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np


BLE_ADV_CHANNELS = {
    37: 2_402_000_000,
    38: 2_426_000_000,
    39: 2_480_000_000,
}
BLE_ADV_ACCESS_BYTES = bytes.fromhex("d6be898e")
BLE_ADV_SAMPLE_RATE_SPS = 2_000_000

PROJECT_ROOT = Path(__file__).resolve().parents[5]
UI_DATA_DIR = PROJECT_ROOT / "ui" / "backend" / "data"
COMPANY_IDENTIFIERS_PATH = UI_DATA_DIR / "company_identifiers.json"
UUID16_IDENTIFIERS_PATH = UI_DATA_DIR / "uuid16_identifiers.json"

UUID16_VENDOR_OVERRIDES = {
    "0xFCB2": "Apple, Inc.",
    "0xFEED": "Tile, Inc.",
}

BLE_APPEARANCE_LABELS = {
    0x0000: "Unknown",
    0x0040: "Phone",
    0x0080: "Computer",
    0x00C0: "Watch",
    0x00C1: "Sports Watch",
    0x0100: "Clock",
    0x0140: "Display",
    0x0180: "Remote",
    0x01C0: "Eye-glasses",
    0x0200: "Tag",
    0x0240: "Keyring",
    0x0280: "Media Player",
    0x02C0: "Barcode Scanner",
    0x0300: "Thermometer",
    0x0340: "Heart Rate Sensor",
    0x0380: "Blood Pressure",
    0x03C0: "HID",
    0x03C1: "Keyboard",
    0x03C2: "Mouse",
    0x03C3: "Joystick",
    0x03C4: "Gamepad",
    0x03C5: "Digitizer Tablet",
    0x03C6: "Card Reader",
    0x03C7: "Digital Pen",
    0x03C8: "Barcode Scanner",
    0x0400: "Glucose Meter",
    0x0440: "Running Sensor",
    0x0441: "Running Sensor Pod",
    0x0442: "Running Sensor Shoe",
    0x0480: "Cycling",
    0x0481: "Cycling Computer",
    0x0482: "Cycling Speed Sensor",
    0x0483: "Cycling Cadence Sensor",
    0x0484: "Cycling Power Sensor",
    0x0485: "Cycling Speed/Cadence Sensor",
    0x04C0: "Pulse Oximeter",
    0x0500: "Weight Scale",
    0x0540: "Personal Mobility",
    0x0580: "Continuous Glucose Monitor",
    0x05C0: "Insulin Pump",
    0x0600: "Medication Delivery",
    0x0640: "Outdoor Sports",
    0x0641: "Location Display Device",
    0x0642: "Location Navigation Device",
    0x0643: "Location Pod",
    0x0644: "Location Beacon",
}


def _load_company_identifier_lut() -> dict[str, str]:
    if not COMPANY_IDENTIFIERS_PATH.exists():
        return {}
    try:
        with COMPANY_IDENTIFIERS_PATH.open("r", encoding="ascii") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    companies = data.get("companies") if isinstance(data, dict) else {}
    if not isinstance(companies, dict):
        return {}
    return {str(key).upper().replace("X", "x"): str(value) for key, value in companies.items()}


COMPANY_IDENTIFIER_LUT = _load_company_identifier_lut()


def company_name(company_id: str) -> str:
    return COMPANY_IDENTIFIER_LUT.get(str(company_id or "").upper().replace("X", "x"), "")


def _load_uuid16_identifier_lut() -> dict[str, str]:
    if not UUID16_IDENTIFIERS_PATH.exists():
        return {}
    try:
        with UUID16_IDENTIFIERS_PATH.open("r", encoding="ascii") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    uuids = data.get("uuids") if isinstance(data, dict) else {}
    if not isinstance(uuids, dict):
        return {}
    return {str(key).upper().replace("X", "x"): str(value) for key, value in uuids.items()}


UUID16_IDENTIFIER_LUT = _load_uuid16_identifier_lut()


def uuid16_name(uuid16: str) -> str:
    key = str(uuid16 or "").upper().replace("X", "x")
    return UUID16_VENDOR_OVERRIDES.get(key) or UUID16_IDENTIFIER_LUT.get(key, "")


def uuid16_names(uuid16_values: list[str]) -> list[str]:
    return list(dict.fromkeys(name for uuid in uuid16_values for name in [uuid16_name(uuid)] if name))


def canonical_ble_vendor(name: str) -> str:
    value = str(name or "").strip()
    lowered = value.lower()
    if "apple" in lowered:
        return "Apple, Inc."
    if "microsoft" in lowered:
        return "Microsoft"
    if "tile" in lowered:
        return "Tile, Inc."
    return value


def manufacturer_from_uuid16(uuid16_values: list[str]) -> dict[str, Any] | None:
    for uuid in uuid16_values:
        name = canonical_ble_vendor(uuid16_name(uuid))
        if not name:
            continue
        return {"company_id": "", "company_name": name, "data": "", "source": "uuid16", "uuid16": str(uuid).upper().replace("X", "x")}
    return None


def ble_identity_label(name: str, uuid16_name_values: list[str], manufacturer: dict[str, Any] | None, mac: str) -> str:
    local_name = str(name or "").strip()
    if local_name:
        return local_name
    manufacturer_name = canonical_ble_vendor(str((manufacturer or {}).get("company_name") or ""))
    manufacturer_source = str((manufacturer or {}).get("source") or "")
    if uuid16_name_values:
        first_uuid_name = str(uuid16_name_values[0] or "").strip()
        if manufacturer_name == "Apple, Inc." and manufacturer_source == "uuid16":
            return "AirTag"
        return first_uuid_name
    if manufacturer_name:
        if manufacturer_name == "Apple, Inc." and manufacturer_source == "uuid16":
            return "AirTag"
        return manufacturer_name
    return mac


def ble_device_type_label(name: str, uuid16_name_values: list[str], manufacturer: dict[str, Any] | None, appearance: dict[str, Any] | None) -> str:
    local_name = str(name or "").strip()
    if local_name:
        return ""
    manufacturer_name = canonical_ble_vendor(str((manufacturer or {}).get("company_name") or ""))
    manufacturer_source = str((manufacturer or {}).get("source") or "")
    if manufacturer_name == "Apple, Inc." and manufacturer_source == "uuid16":
        return "AirTag"
    if appearance and str(appearance.get("label") or "").strip():
        return str(appearance.get("label") or "").strip()
    if manufacturer_name == "Tile, Inc.":
        return "Tracker"
    return ""


def ble_device_type_detail(uuid16_name_values: list[str], manufacturer: dict[str, Any] | None, appearance: dict[str, Any] | None) -> str:
    manufacturer_name = canonical_ble_vendor(str((manufacturer or {}).get("company_name") or ""))
    manufacturer_source = str((manufacturer or {}).get("source") or "")
    manufacturer_data = str((manufacturer or {}).get("data") or "").upper()
    if manufacturer_name == "Apple, Inc." and manufacturer_source == "uuid16":
        return "Find My UUID16"
    if manufacturer_name == "Apple, Inc." and manufacturer_data:
        prefix = manufacturer_data[:4]
        if prefix == "1202":
            return "Find My manufacturer frame"
        if prefix in {"1005", "1003", "1001"}:
            return "Continuity frame"
        return f"Apple manufacturer frame {prefix}" if prefix else "Apple manufacturer frame"
    if manufacturer_name == "Microsoft" and manufacturer_data:
        prefix = manufacturer_data[:2]
        if prefix == "03":
            return "Swift Pair frame"
        return f"Microsoft manufacturer frame {manufacturer_data[:4]}" if manufacturer_data else "Microsoft manufacturer frame"
    if manufacturer_name == "Tile, Inc.":
        if uuid16_name_values:
            return "Tile UUID16 service"
        if manufacturer_data:
            return "Tile manufacturer frame"
        return "Tile tracker"
    if appearance and str(appearance.get("code") or "").strip():
        return str(appearance.get("code") or "").strip()
    return ""


def ble_identity_source(name: str, uuid16_name_values: list[str], manufacturer: dict[str, Any] | None) -> str:
    manufacturer_name = str((manufacturer or {}).get("company_name") or "")
    if name:
        return "Local name"
    if uuid16_name_values:
        label = uuid16_name_values[0]
        if canonical_ble_vendor(label) == "Apple, Inc.":
            return "AirTag inferred from UUID16 service"
        return f"{label} UUID16 service"
    if manufacturer_name:
        if (manufacturer or {}).get("source") == "uuid16":
            if canonical_ble_vendor(manufacturer_name) == "Apple, Inc.":
                return "AirTag inferred from UUID16 service"
            return f"{manufacturer_name} inferred from UUID16 service"
        return f"{manufacturer_name} manufacturer data"
    return "MAC only"


def enrich_ble_event(event: dict[str, Any]) -> dict[str, Any]:
    out = dict(event)
    uuid16_values = [str(item) for item in out.get("uuid16") or []]
    uuid16_name_values = uuid16_names(uuid16_values)
    manufacturer = out.get("manufacturer") if isinstance(out.get("manufacturer"), dict) else None
    if not manufacturer:
        manufacturer = manufacturer_from_uuid16(uuid16_values)
    out["manufacturer"] = manufacturer
    out["uuid16_names"] = uuid16_name_values
    out["identity"] = ble_identity_label(str(out.get("name") or ""), uuid16_name_values, manufacturer, str(out.get("address") or ""))
    out["identity_source"] = ble_identity_source(str(out.get("name") or ""), uuid16_name_values, manufacturer)
    out["device_type"] = ble_device_type_label(
        str(out.get("name") or ""),
        uuid16_name_values,
        manufacturer,
        out.get("appearance") if isinstance(out.get("appearance"), dict) else None,
    )
    out["device_type_detail"] = ble_device_type_detail(
        uuid16_name_values,
        manufacturer,
        out.get("appearance") if isinstance(out.get("appearance"), dict) else None,
    )
    return out


class BLEAdvertisingDetector:
    # Longest legacy advertising packet after the access address: 2-byte header + 37-byte PDU + 3-byte CRC.
    MAX_PACKET_BITS = (2 + 37 + 3) * 8
    MAX_AA_ERRORS = 1

    def __init__(self, sample_rate_sps: int = BLE_ADV_SAMPLE_RATE_SPS, center_freq_hz: int = BLE_ADV_CHANNELS[37], channel: int = 37) -> None:
        self.sample_rate_sps = int(sample_rate_sps)
        self.center_freq_hz = int(center_freq_hz)
        self.channel = int(channel)
        self.sps = max(1, int(round(self.sample_rate_sps / 1_000_000.0)))
        # Samples carried into the next chunk so packets that straddle a chunk boundary still decode.
        self._tail = np.empty(0, dtype=np.complex64)
        self._tail_len = (self.MAX_PACKET_BITS + 48) * self.sps
        self._sample_index = 0  # stream position of the next chunk's first sample
        self._seen_packet_keys: dict[str, int] = {}
        self._burst_holdoff = 0
        self._aa_pm = np.array([1 if b else -1 for b in self._bytes_to_lsb_bits(BLE_ADV_ACCESS_BYTES)], dtype=np.float32)
        self._whitening = np.array(self._ble_dewhiten_bits([0] * self.MAX_PACKET_BITS, self.channel), dtype=np.int8)
        self.stats: dict[str, int] = {key: 0 for key in (
            "samples", "chunks", "clipped_samples", "bursts", "aa_candidates", "header_rejects", "length_rejects",
            "truncated", "crc_failures", "crc_valid", "duplicates", "packets")}

    def process_iq_i8(self, raw: bytes) -> tuple[float, list[dict[str, Any]]]:
        z = self._iq_bytes_to_complex(raw)
        if z.size < 64:
            return -120.0, []
        iq = np.frombuffer(raw[: z.size * 2], dtype=np.int8)
        self.stats["clipped_samples"] += int(np.count_nonzero((iq[0::2] <= -127) | (iq[0::2] >= 127)
                                                              | (iq[1::2] <= -127) | (iq[1::2] >= 127)))
        return self.process_complex(z)

    def process_complex(self, z: np.ndarray) -> tuple[float, list[dict[str, Any]]]:
        power = np.abs(z) ** 2
        rssi = float(10.0 * np.log10(float(np.mean(power)) + 1e-12))
        threshold = max(float(np.median(power) * 6.5), float(np.mean(power) * 2.2))
        burst_spans = self._find_bursts(power, threshold)
        return rssi, self._ble_events(z, rssi, burst_spans)

    def _iq_bytes_to_complex(self, raw: bytes) -> np.ndarray:
        if len(raw) < 4:
            return np.empty(0, dtype=np.complex64)
        if len(raw) % 2:
            raw = raw[:-1]
        iq = np.frombuffer(raw, dtype=np.int8).astype(np.float32) / 128.0
        return (iq[0::2] + 1j * iq[1::2]).astype(np.complex64)

    def _find_bursts(self, power: np.ndarray, threshold: float) -> list[tuple[int, int, float]]:
        active = power > threshold
        if not np.any(active):
            self._burst_holdoff = max(0, self._burst_holdoff - power.size)
            return []
        idx = np.flatnonzero(active)
        splits = np.where(np.diff(idx) > max(8, int(self.sample_rate_sps * 0.000012)))[0] + 1
        groups = np.split(idx, splits)
        spans: list[tuple[int, int, float]] = []
        min_len = max(20, int(self.sample_rate_sps * 0.000018))
        for group in groups:
            if group.size < min_len:
                continue
            start = int(group[0])
            stop = int(group[-1])
            if self._burst_holdoff > start:
                continue
            peak = float(10.0 * np.log10(float(np.max(power[start : stop + 1])) + 1e-12))
            spans.append((start, stop, peak))
            self._burst_holdoff = stop + int(self.sample_rate_sps * 0.000050)
        self._burst_holdoff = max(0, self._burst_holdoff - power.size)
        return spans[:12]

    def _ble_events(self, z: np.ndarray, rssi_dbfs: float, burst_spans: list[tuple[int, int, float]]) -> list[dict[str, Any]]:
        self.stats["samples"] += int(z.size)
        self.stats["chunks"] += 1
        self.stats["bursts"] += len(burst_spans)
        buffer = np.concatenate((self._tail, z.astype(np.complex64, copy=False)))
        buffer_start = self._sample_index - self._tail.size
        self._tail = buffer[-self._tail_len:].copy()
        self._sample_index += int(z.size)
        events = self._decode_buffer(buffer, buffer_start, rssi_dbfs)
        if not events:
            events.extend(self._burst_only_events(rssi_dbfs, burst_spans))
        return events[:64]

    def _soft_symbols(self, buffer: np.ndarray) -> np.ndarray:
        """Amplitude-independent FM discriminator, integrated over one symbol starting at every sample."""
        freq = np.angle(buffer[1:] * np.conj(buffer[:-1])).astype(np.float32)
        if self.sps == 1:
            return freq
        csum = np.concatenate(([0.0], np.cumsum(freq, dtype=np.float64)))
        return (csum[self.sps:] - csum[:-self.sps]).astype(np.float32)

    def _decode_buffer(self, buffer: np.ndarray, buffer_start: int, rssi_dbfs: float) -> list[dict[str, Any]]:
        if buffer.size < 64 * self.sps:
            return []
        integrated = self._soft_symbols(buffer)
        aa_len = self._aa_pm.size
        # Gather access-address hits at every timing phase, from both slicers.
        hits: dict[tuple[int, int, bool], float] = {}
        softs = []
        for phase in range(self.sps):
            soft = integrated[phase:: self.sps]
            softs.append(soft)
            if soft.size < aa_len + 16:
                continue
            # Slice against zero, and against a forward local mean that follows carrier frequency offset.
            window = 32
            csum = np.concatenate(([0.0], np.cumsum(soft, dtype=np.float64)))
            fwd = np.empty(soft.size, dtype=np.float32)
            n_full = soft.size - window + 1
            fwd[:n_full] = (csum[window:] - csum[:n_full]) / window
            fwd[n_full:] = fwd[n_full - 1] if n_full > 0 else 0.0
            for sliced in (soft, soft - fwd):
                corr = np.correlate(np.where(sliced > 0, 1.0, -1.0).astype(np.float32), self._aa_pm, mode="valid")
                errors = (aa_len - corr) / 2.0
                for pos in np.flatnonzero((errors <= self.MAX_AA_ERRORS) | (errors >= aa_len - self.MAX_AA_ERRORS)):
                    inverted = bool(errors[pos] >= aa_len - self.MAX_AA_ERRORS)
                    if (phase, int(pos), inverted) in hits:
                        continue
                    aa = soft[pos: pos + aa_len] * (-1.0 if inverted else 1.0)
                    # Eye opening on the known access address: higher means better symbol timing.
                    hits[(phase, int(pos), inverted)] = float(np.dot(aa, self._aa_pm) / (np.abs(aa).sum() + 1e-9))
        # Group hits of the same packet (neighbouring phases/symbols); try the best-aligned first.
        groups: list[list[tuple[int, int, bool]]] = []
        for key in sorted(hits, key=lambda k: (k[1] * self.sps + k[0])):
            sample_pos = key[1] * self.sps + key[0]
            if groups and sample_pos - (groups[-1][0][1] * self.sps + groups[-1][0][0]) <= 2 * self.sps:
                groups[-1].append(key)
            else:
                groups.append([key])
        out: list[dict[str, Any]] = []
        for group in groups:
            self.stats["aa_candidates"] += 1
            failure = None
            for phase, pos, inverted in sorted(group, key=lambda k: -hits[k]):
                event, failure = self._decode_candidate(softs[phase], pos, inverted, buffer_start + pos * self.sps + phase, rssi_dbfs)
                if failure is None:
                    if event is not None:
                        out.append(event)
                    break
            else:
                self.stats[failure] += 1
        return out

    def _decode_candidate(self, soft: np.ndarray, pos: int, inverted: bool, stream_pos: int,
                          rssi_dbfs: float) -> tuple[dict[str, Any] | None, str | None]:
        """Returns (event, None) on a CRC-valid packet (event None if it is a repeat), else (None, failure reason)."""
        aa_len = self._aa_pm.size
        data = soft[pos + aa_len: pos + aa_len + self.MAX_PACKET_BITS]
        if data.size < 16:
            return None, "truncated"
        # Decision threshold from the known access address: midway between the mean "1" and mean "0" levels.
        aa_soft = soft[pos: pos + aa_len]
        sign = -1.0 if inverted else 1.0
        ones, zeros = aa_soft[self._aa_pm * sign > 0], aa_soft[self._aa_pm * sign < 0]
        threshold = float((ones.mean() + zeros.mean()) / 2.0) if ones.size and zeros.size else 0.0
        failure = "header_rejects"
        for level in (threshold, 0.0):
            raw = (data > level).astype(np.int8) ^ np.int8(inverted)
            bits = raw ^ self._whitening[: raw.size]
            header = self._bits_to_bytes(bits[:16].tolist())
            pdu_type, length = header[0] & 0x0F, header[1] & 0x3F
            if pdu_type > 6:
                continue
            if length < 6 or length > 37:
                failure = "length_rejects" if failure == "header_rejects" else failure
                continue
            packet_bits = (2 + length + 3) * 8
            if bits.size < packet_bits:
                failure = "truncated"
                continue
            pdu_bits = bits[: (2 + length) * 8].tolist()
            if self._ble_crc24_bits(pdu_bits) != bits[(2 + length) * 8: packet_bits].tolist():
                failure = "crc_failures"
                continue
            self.stats["crc_valid"] += 1
            return self._packet_event(self._bits_to_bytes(pdu_bits), header[0], length, stream_pos, rssi_dbfs), None
        return None, failure

    def _packet_event(self, packet: bytes, header0: int, length: int, stream_pos: int, rssi_dbfs: float) -> dict[str, Any] | None:
        pdu_type = header0 & 0x0F
        body = packet[2: 2 + length]
        advertiser = self._format_ble_addr(body[:6])
        key = f"own-crc:{self.channel}:{packet.hex()}"
        # Suppress repeats of an identical packet within one second of stream time (not wall time),
        # so live and recorded IQ behave the same.
        last = self._seen_packet_keys.get(key)
        if last is not None and stream_pos - last < self.sample_rate_sps:
            self.stats["duplicates"] += 1
            return None
        self._seen_packet_keys[key] = stream_pos
        if len(self._seen_packet_keys) > 4096:
            horizon = stream_pos - self.sample_rate_sps
            self._seen_packet_keys = {k: v for k, v in self._seen_packet_keys.items() if v >= horizon}
        self.stats["packets"] += 1
        ad_fields = self._ble_ad_fields(body[6:] if len(body) > 6 else b"")
        return enrich_ble_event({
            "kind": "ble_adv",
            "seen_at": time.time(),
            "channel": self.channel,
            "center_freq_hz": self.center_freq_hz,
            "rssi_dbfs": round(rssi_dbfs, 1),
            "pdu_type": self._ble_pdu_name(pdu_type),
            "pdu_type_id": pdu_type,
            "address": advertiser or "unknown",
            "address_type": "random" if header0 & 0x40 else "public",
            "name": self._ble_local_name_from_fields(ad_fields),
            "uuid16": self._ble_uuid16s_from_fields(ad_fields),
            "manufacturer": self._ble_manufacturer_from_fields(ad_fields),
            "appearance": self._ble_appearance_from_fields(ad_fields),
            "payload_len": length,
            "confidence": 0.94,
            "decoder": "own-crc",
            "packet": packet.hex(),
        })

    def _burst_only_events(self, rssi_dbfs: float, burst_spans: list[tuple[int, int, float]]) -> list[dict[str, Any]]:
        return [
            {
                "kind": "ble_burst",
                "seen_at": time.time(),
                "channel": self.channel,
                "center_freq_hz": self.center_freq_hz,
                "rssi_dbfs": round(rssi_dbfs, 1),
                "peak_dbfs": round(peak, 1),
                "confidence": 0.28,
            }
            for _, _, peak in burst_spans[:8]
        ]

    @staticmethod
    def _ble_dewhiten_bits(bits: list[int], channel: int) -> list[int]:
        lfsr = [1, (channel >> 5) & 1, (channel >> 4) & 1, (channel >> 3) & 1, (channel >> 2) & 1, (channel >> 1) & 1, channel & 1]
        out: list[int] = []
        for raw_bit in bits:
            out.append((raw_bit & 1) ^ lfsr[6])
            lfsr = [lfsr[6], lfsr[0], lfsr[1], lfsr[2], lfsr[3] ^ lfsr[6], lfsr[4], lfsr[5]]
        return out

    @staticmethod
    def _ble_crc24_bits(bits: list[int]) -> list[int]:
        state = [1, 0] * 12
        for bit in bits:
            new_bit = state[23] ^ (bit & 1)
            state = [
                new_bit, state[0] ^ new_bit, state[1], state[2] ^ new_bit,
                state[3] ^ new_bit, state[4], state[5] ^ new_bit, state[6],
                state[7], state[8] ^ new_bit, state[9] ^ new_bit, state[10],
                state[11], state[12], state[13], state[14], state[15], state[16],
                state[17], state[18], state[19], state[20], state[21], state[22],
            ]
        return list(reversed(state))

    @staticmethod
    def _ble_ad_fields(ad_data: bytes) -> list[tuple[int, bytes]]:
        idx = 0
        fields: list[tuple[int, bytes]] = []
        while idx < len(ad_data):
            field_len = ad_data[idx]
            if field_len == 0:
                break
            field_end = idx + 1 + field_len
            if field_end > len(ad_data):
                break
            fields.append((ad_data[idx + 1], ad_data[idx + 2 : field_end]))
            idx = field_end
        return fields

    @staticmethod
    def _ble_local_name_from_fields(fields: list[tuple[int, bytes]]) -> str:
        best = ""
        for ad_type, value in fields:
            if ad_type in {0x08, 0x09} and value:
                name = value.decode("utf-8", errors="replace").strip("\x00\r\n\t ")
                if name:
                    best = name
                    if ad_type == 0x09:
                        return best
        return best

    @staticmethod
    def _ble_uuid16s_from_fields(fields: list[tuple[int, bytes]]) -> list[str]:
        uuids: list[str] = []
        for ad_type, value in fields:
            if ad_type in {0x02, 0x03, 0x14}:
                for idx in range(0, len(value) - 1, 2):
                    uuids.append(f"0x{int.from_bytes(value[idx : idx + 2], 'little'):04X}")
            elif ad_type == 0x16 and len(value) >= 2:
                uuids.append(f"0x{int.from_bytes(value[:2], 'little'):04X}")
        return uuids

    @staticmethod
    def _ble_manufacturer_from_fields(fields: list[tuple[int, bytes]]) -> dict[str, Any] | None:
        for ad_type, value in fields:
            if ad_type != 0xFF or len(value) < 2:
                continue
            company_id = int.from_bytes(value[:2], "little")
            company_hex = f"0x{company_id:04X}"
            return {"company_id": company_hex, "company_name": company_name(company_hex), "data": value[2:].hex().upper()}
        return None

    @staticmethod
    def _ble_appearance_from_fields(fields: list[tuple[int, bytes]]) -> dict[str, Any] | None:
        for ad_type, value in fields:
            if ad_type != 0x19 or len(value) < 2:
                continue
            code = int.from_bytes(value[:2], "little")
            return {"code": f"0x{code:04X}", "label": BLE_APPEARANCE_LABELS.get(code, f"Appearance {code:#06x}")}
        return None

    @staticmethod
    def _bytes_to_lsb_bits(data: bytes) -> list[int]:
        return [(byte >> bit) & 1 for byte in data for bit in range(8)]

    @staticmethod
    def _bits_to_bytes(bits: list[int]) -> bytes:
        usable = (len(bits) // 8) * 8
        out = bytearray()
        for idx in range(0, usable, 8):
            value = 0
            for bit in range(8):
                value |= (bits[idx + bit] & 1) << bit
            out.append(value)
        return bytes(out)

    @staticmethod
    def _format_ble_addr(raw: bytes) -> str:
        return ":".join(f"{byte:02X}" for byte in raw[::-1])

    @staticmethod
    def _ble_pdu_name(pdu_type: int) -> str:
        names = {0: "ADV_IND", 1: "ADV_DIRECT_IND", 2: "ADV_NONCONN_IND", 3: "SCAN_REQ", 4: "SCAN_RSP", 5: "CONNECT_IND", 6: "ADV_SCAN_IND"}
        return names.get(pdu_type, f"PDU_{pdu_type}")


class WideBLEAdvertisingDetector:
    """Channelize a wide 2.4 GHz capture into BLE advertising lanes."""

    def __init__(
        self,
        sample_rate_sps: int,
        center_freq_hz: int,
        channels: list[int] | None = None,
        channel_rate_sps: int = BLE_ADV_SAMPLE_RATE_SPS,
        guard_hz: int = 1_200_000,
    ) -> None:
        self.sample_rate_sps = int(sample_rate_sps)
        self.center_freq_hz = int(center_freq_hz)
        self.channel_rate_sps = int(channel_rate_sps)
        self.decim = max(1, int(round(self.sample_rate_sps / float(self.channel_rate_sps))))
        self.lanes: list[dict[str, Any]] = []
        requested = channels or sorted(BLE_ADV_CHANNELS)
        for channel in requested:
            freq_hz = BLE_ADV_CHANNELS[int(channel)]
            offset_hz = float(freq_hz - self.center_freq_hz)
            if abs(offset_hz) > (self.sample_rate_sps / 2.0) - float(guard_hz):
                continue
            self.lanes.append(
                {
                    "channel": int(channel),
                    "freq_hz": int(freq_hz),
                    "offset_hz": offset_hz,
                    "mix_phase_rad": 0.0,
                    "detector": BLEAdvertisingDetector(self.channel_rate_sps, freq_hz, int(channel)),
                }
            )

    def process_iq_i8(self, raw: bytes) -> tuple[float, list[dict[str, Any]]]:
        z = BLEAdvertisingDetector._iq_bytes_to_complex(self, raw)
        if z.size < 64:
            return -120.0, []
        return self.process_complex(z)

    def process_complex(self, z: np.ndarray) -> tuple[float, list[dict[str, Any]]]:
        rssis: list[float] = []
        events: list[dict[str, Any]] = []
        sample_idx = np.arange(z.size, dtype=np.float32)
        for lane in self.lanes:
            phase_step = float((-2.0 * np.pi * float(lane["offset_hz"])) / float(self.sample_rate_sps))
            phase0 = float(lane.get("mix_phase_rad", 0.0))
            rot = np.exp(1j * (phase0 + (phase_step * sample_idx))).astype(np.complex64)
            lane["mix_phase_rad"] = float((phase0 + (phase_step * float(z.size))) % (2.0 * np.pi))
            mixed = z * rot
            lane_samples = self._decimate(mixed, self.decim)
            if lane_samples.size < 64:
                continue
            rssi, lane_events = lane["detector"].process_complex(lane_samples)
            rssis.append(rssi)
            events.extend(lane_events)
        events.sort(key=lambda item: float(item.get("seen_at") or 0.0), reverse=True)
        return max(rssis) if rssis else -120.0, events[:48]

    @staticmethod
    def _decimate(z: np.ndarray, decim: int) -> np.ndarray:
        if decim <= 1:
            return z.astype(np.complex64, copy=False)
        usable = (z.size // decim) * decim
        if usable <= 0:
            return np.empty(0, dtype=np.complex64)
        return z[:usable].reshape(-1, decim).mean(axis=1).astype(np.complex64)
