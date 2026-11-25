"""BLE advertising decoder on synthetic GFSK packets with known contents (no radio required)."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rf_platform/plugins/bluetooth-lowenergy/src"))
from bluetooth_lowenergy.detector import BLE_ADV_ACCESS_BYTES, BLEAdvertisingDetector as D  # noqa: E402

RATE = 4_000_000


def air_bits(channel: int, pdu: bytes) -> list[int]:
    """Preamble + access address + whitened PDU and CRC, in transmission order."""
    pdu_bits = D._bytes_to_lsb_bits(pdu)
    whitened = D._ble_dewhiten_bits(pdu_bits + D._ble_crc24_bits(pdu_bits), channel)
    aa = D._bytes_to_lsb_bits(BLE_ADV_ACCESS_BYTES)
    preamble = [1 - aa[0] if i % 2 == 0 else aa[0] for i in range(8)]
    return preamble + aa + whitened


def gfsk(bits: list[int], cfo_hz: float, sps: int = 4) -> np.ndarray:
    nrz = np.repeat(np.array(bits) * 2 - 1.0, sps)
    t = np.arange(-2 * sps, 2 * sps + 1) / sps
    taps = np.exp(-2 * (np.pi * 0.5 * t) ** 2 / np.log(2))  # BT = 0.5
    freq = np.convolve(nrz, taps / taps.sum(), mode="same")
    return np.exp(1j * np.cumsum(np.pi * 0.5 * freq / sps + 2 * np.pi * cfo_hz / (sps * 1e6)))  # h = 0.5


def capture(channel: int, count: int, snr_db: float, cfo_hz: float, delay: float, seed: int = 7,
            corrupt_bits: int = 0):
    """Synthetic capture; with corrupt_bits, that many bits after the header are flipped in every packet."""
    rng = np.random.default_rng(seed)
    parts, truth = [], set()
    for _ in range(count):
        length = int(rng.integers(6, 38))
        pdu = bytes([0x42, length]) + bytes(rng.integers(0, 256, length, dtype=np.uint8))
        truth.add(pdu.hex())
        bits = air_bits(channel, pdu)
        for pos in rng.choice(np.arange(40 + 16, len(bits)), corrupt_bits, replace=False):
            bits[pos] ^= 1
        parts += [np.zeros(int(rng.integers(3000, 9000)), complex), gfsk(bits, cfo_hz)]
    x = np.concatenate(parts + [np.zeros(4000, complex)])
    if delay:  # fractional-sample timing offset
        n = np.arange(x.size) + delay
        x = np.interp(n, np.arange(x.size), x.real) + 1j * np.interp(n, np.arange(x.size), x.imag)
    x = 0.3 * (x + (rng.normal(size=x.size) + 1j * rng.normal(size=x.size)) / np.sqrt(2) * 10 ** (-snr_db / 20))
    iq = np.empty(2 * x.size, np.int8)
    iq[0::2], iq[1::2] = np.clip(np.round(x.real * 127), -128, 127), np.clip(np.round(x.imag * 127), -128, 127)
    return iq.tobytes(), truth


def decode(raw: bytes, channel: int, chunk_samples: int):
    detector = D(sample_rate_sps=RATE, center_freq_hz=2_402_000_000, channel=channel)
    found = set()
    for i in range(0, len(raw), 2 * chunk_samples):
        found |= {e["packet"] for e in detector.process_iq_i8(raw[i: i + 2 * chunk_samples])[1] if e["kind"] == "ble_adv"}
    return found, detector.stats


class BLEDecoderTest(unittest.TestCase):
    def test_decodes_every_packet_across_offsets_and_chunk_boundaries(self):
        for snr_db, cfo_hz, delay in ((20, 0, 0), (15, 60_000, 0.5), (15, -120_000, 1.3)):
            raw, truth = capture(37, 40, snr_db, cfo_hz, delay)
            for chunk in (16_384, 131_072):  # small chunks put many packets across boundaries
                found, stats = decode(raw, 37, chunk)
                self.assertEqual(found, truth, (snr_db, cfo_hz, delay, chunk))
                self.assertEqual(stats["packets"], len(truth))

    def test_wrong_channel_whitening_finds_nothing(self):
        raw, _ = capture(38, 40, 20, 0, 0)
        found, stats = decode(raw, 37, 131_072)
        self.assertEqual(found, set())  # every candidate fails CRC: no false detections
        self.assertGreater(stats["aa_candidates"], 0)
        self.assertEqual(stats["crc_failures"] + stats["header_rejects"] + stats["length_rejects"]
                         + stats["truncated"], stats["aa_candidates"])

    def test_corrupted_packets_are_never_reported(self):
        for flips in (1, 2, 3):
            raw, _ = capture(37, 40, 25, 30_000, 0.5, seed=flips, corrupt_bits=flips)
            found, stats = decode(raw, 37, 131_072)
            self.assertEqual(found, set(), flips)
            self.assertEqual(stats["crc_valid"], 0)
            self.assertGreaterEqual(stats["aa_candidates"], 40)  # every packet synchronised, none accepted

    def test_low_snr_reports_only_transmitted_packets(self):
        for snr_db in (4, 6, 8):
            raw, truth = capture(38, 40, snr_db, -50_000, 1.1, seed=snr_db)
            found, _ = decode(raw, 38, 131_072)
            self.assertLessEqual(found, truth, snr_db)  # fewer decodes near threshold, but never invented ones

    def test_noise_produces_no_detections(self):
        rng = np.random.default_rng(3)
        raw = np.clip(np.round(rng.normal(scale=20, size=2 * RATE)), -128, 127).astype(np.int8).tobytes()
        found, _ = decode(raw, 37, 131_072)
        self.assertEqual(found, set())


class DashboardParityTest(unittest.TestCase):
    """The dashboard decodes BLE with the same decoder as the agent: identical detections and counters."""

    @classmethod
    def setUpClass(cls):
        from tests.test_validation_harness import BackendValidationTest
        BackendValidationTest.setUpClass()
        cls.harness, cls.backend = BackendValidationTest, BackendValidationTest.backend

    @classmethod
    def tearDownClass(cls):
        cls.harness.tearDownClass()

    def test_dashboard_and_agent_decoders_agree(self):
        raw, truth = capture(37, 30, 15, 40_000, 0.7)
        agent_found, agent_stats = decode(raw, 37, 131_072)
        dashboard = self.backend.BluetoothDetector(RATE, "ble", 2_402_000_000, 37)
        found, events = set(), []
        for i in range(0, len(raw), 2 * 131_072):
            _, out, candidates = dashboard.process_iq_i8(raw[i: i + 2 * 131_072])
            self.assertEqual(candidates, [])
            events += [e for e in out if e["kind"] == "ble_adv"]
        self.assertEqual({e["packet"] for e in events}, truth)
        self.assertEqual({e["packet"] for e in events}, agent_found)
        self.assertEqual(dashboard.stats, agent_stats)
        self.assertTrue(all(e["decoder"] == "own-crc" and "identity" in e for e in events))


if __name__ == "__main__":
    unittest.main()
