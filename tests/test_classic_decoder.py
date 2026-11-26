"""Bluetooth Classic decoder on synthetic access codes with known LAPs (needs the built native decoder)."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from synth_classic_iq import drop_samples, synthesize, sync_word, to_cs8  # noqa: E402

DECODER = ROOT / "rf_platform/plugins/bluetooth-classic/build/btcexplorer-sniffer-gateway"


def access_hits(iq: bytes, bandwidth_mhz: int = 20) -> int:
    with tempfile.TemporaryDirectory() as temp:
        silence = b"\0" * (2 * bandwidth_mhz * 100_000)  # 100 ms
        proc = subprocess.Popen([str(DECODER), "--input-stdin", "--input-format", "cs8", "--freq-mhz", "2442",
                                 "--bandwidth-mhz", str(bandwidth_mhz), "--seconds", "0.5", "--jsonl-stdout",
                                 "--log", str(Path(temp) / "sniffer.log")],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        proc.stdin.write(iq)
        # Metrics are emitted after a bank only when 1 s of wall time has passed, and there is no final
        # report, so keep feeding silence until a few seconds have elapsed after the data.
        deadline = time.monotonic() + 3.5
        while time.monotonic() < deadline:
            proc.stdin.write(silence)
        proc.stdin.close()
        out = proc.stdout.read().decode(errors="replace")
        proc.wait(timeout=60)
    hits = 0
    for line in out.splitlines():
        start = line.find('{"time_us"')
        if start >= 0 and '"type":"metrics"' in line:
            hits = json.loads(line[start:])["access_hits"]
    return hits


@unittest.skipUnless(DECODER.exists(), "native Bluetooth Classic decoder not built")
class ClassicDecoderTest(unittest.TestCase):
    def test_sync_word_matches_the_decoder(self):
        # Reference values computed by the decoder's own construction (btsniffer.cpp).
        self.assertEqual(sync_word(0x123456), 0xB048D15A658627C0)
        self.assertEqual(sync_word(0xE2FE32), 0x4F8BF8C99D7775E2)

    def test_continuous_stream_beats_one_with_gaps(self):
        x, truth = synthesize([0x123456, 0xC0FFEE], 100, 2442, 20, snr_db=15.0, seed=4)
        clean = access_hits(to_cs8(x))
        gapped = access_hits(to_cs8(drop_samples(x, 508, 20)))
        self.assertGreater(clean, 0)
        self.assertLessEqual(clean, len(truth))
        self.assertLess(gapped, clean)


if __name__ == "__main__":
    unittest.main()
