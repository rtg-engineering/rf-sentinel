#!/usr/bin/env python3
"""Synthetic Bluetooth Classic (BR) IQ with known LAPs, for testing the Classic decoder without live traffic.

  python3 scripts/synth_classic_iq.py --out .demo/iq/synth-classic.cs8 --laps 9E8B33 123456 C0FFEE \\
      --packets-per-lap 40 --snr-db 15 [--gap-every 508 --gap-samples 20]

Each packet is a basic-rate burst: 4-bit preamble, the LAP's 64-bit sync word, 4-bit trailer, then a
54-bit header and a payload of random bits, GFSK at 1 Msym/s (BT 0.5, modulation index 0.32), on a
random 1 MHz Classic channel inside the captured band. Header and payload are not valid FEC/HEC words,
so this exercises access-code (LAP) detection, not UAP resolution.

--gap-every/--gap-samples delete samples periodically to imitate a stream that loses data (the
SoapyBladeRF overrun after every 508-sample message). Writes <out> (cs8) and <out>.json (the truth).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def _length(word: int, left: int = 64, right: int = 0) -> int:
    """Port of the decoder's length() (btsniffer.cpp): a binary search that returns bit_length - 1."""
    if left == right:
        return left
    mid = (left + right) // 2
    if right == mid:
        return mid
    return _length(word, left, mid) if word >= (1 << mid) else _length(word, mid, right)


def compute_remainder(value: int, divisor: int) -> int:
    """Port of the decoder's compute_remainder(), so generated sync words are the ones it accepts."""
    mask = (1 << 64) - 1
    divisor_length = _length(divisor)
    if divisor_length + _length(value) > 63:
        return value
    value = (value << divisor_length) & mask
    while _length(value) >= divisor_length:
        value ^= (divisor << (_length(value) - divisor_length)) & mask
    return value


def sync_word(lap: int) -> int:
    """64-bit Classic sync word for a LAP (Bluetooth Core, Baseband 6.3.3); bit k is sent k-th."""
    barker = 0x13 if lap & 0x800000 else 0x2C
    x = (barker << 24) | lap
    pn = 0x83848D96BBCC54FC
    xtilde = (pn >> 34) ^ x
    generator = (int("157464165547", 8) << 1) ^ int("157464165547", 8)
    return ((compute_remainder(xtilde, generator) | (xtilde << 34)) ^ pn) & ((1 << 64) - 1)


def access_code_bits(lap: int) -> list[int]:
    word = sync_word(lap)
    sync = [(word >> k) & 1 for k in range(64)]
    preamble = [sync[0] ^ ((4 - i) % 2) for i in range(4)]       # alternates into the first sync bit
    trailer = [sync[63] ^ ((i + 1) % 2) for i in range(4)]       # alternates out of the last sync bit
    return preamble + sync + trailer


def gfsk(bits: list[int], sps: int, h: float = 0.32, bt: float = 0.5) -> np.ndarray:
    nrz = np.repeat(np.array(bits, dtype=np.float64) * 2 - 1, sps)
    t = np.arange(-2 * sps, 2 * sps + 1) / sps
    taps = np.exp(-2 * (np.pi * bt * t) ** 2 / np.log(2))
    freq = np.convolve(nrz, taps / taps.sum(), mode="same")
    return np.exp(1j * np.cumsum(np.pi * h * freq / sps))


def synthesize(laps: list[int], packets_per_lap: int, center_mhz: int, bandwidth_mhz: int, snr_db: float,
               seed: int = 1, payload_bits: int = 240) -> tuple[np.ndarray, list[dict]]:
    rng = np.random.default_rng(seed)
    rate = bandwidth_mhz * 1_000_000
    sps = bandwidth_mhz
    first = center_mhz - bandwidth_mhz // 2
    channels = [f - 2402 for f in range(first + 1, first + bandwidth_mhz - 1) if 0 <= f - 2402 <= 78]  # skip edge bins
    order = [lap for lap in laps for _ in range(packets_per_lap)]
    rng.shuffle(order)
    gap = int(rate * 625e-6)          # one 625 us slot between packets
    total = len(order) * gap + 2 * gap
    x = np.zeros(total, dtype=np.complex128)
    truth = []
    for i, lap in enumerate(order):
        channel = int(rng.choice(channels))
        bits = access_code_bits(lap) + list(rng.integers(0, 2, 54 + payload_bits))
        burst = gfsk(bits, sps) * np.exp(2j * np.pi * ((2402 + channel) - center_mhz) * 1e6
                                         * np.arange(len(bits) * sps) / rate)
        start = gap + i * gap + int(rng.integers(0, sps))
        x[start: start + burst.size] += burst
        truth.append({"lap": f"{lap:06X}", "channel": channel, "start_sample": start})
    noise = (rng.normal(size=total) + 1j * rng.normal(size=total)) / np.sqrt(2)
    # SNR per Classic channel: noise spread over the whole band, signal in about 1 MHz.
    x = x + noise * 10 ** (-snr_db / 20) * np.sqrt(bandwidth_mhz)
    return x / np.max(np.abs(x)) * 0.9, truth


def drop_samples(x: np.ndarray, every: int, count: int) -> np.ndarray:
    """Keep `every` samples, then delete `count`, repeatedly."""
    if every <= 0 or count <= 0:
        return x
    period = every + count
    keep = (np.arange(x.size) % period) < every
    return x[keep]


def to_cs8(x: np.ndarray) -> bytes:
    iq = np.empty(2 * x.size, dtype=np.int8)
    iq[0::2] = np.clip(np.round(x.real * 127), -128, 127)
    iq[1::2] = np.clip(np.round(x.imag * 127), -128, 127)
    return iq.tobytes()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--laps", nargs="+", default=["9E8B33", "123456", "C0FFEE"])
    parser.add_argument("--packets-per-lap", type=int, default=40)
    parser.add_argument("--center-mhz", type=int, default=2442)
    parser.add_argument("--bandwidth-mhz", type=int, default=20)
    parser.add_argument("--snr-db", type=float, default=15.0)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--gap-every", type=int, default=0)
    parser.add_argument("--gap-samples", type=int, default=0)
    args = parser.parse_args()
    laps = [int(v, 16) for v in args.laps]
    x, truth = synthesize(laps, args.packets_per_lap, args.center_mhz, args.bandwidth_mhz, args.snr_db, args.seed)
    x = drop_samples(x, args.gap_every, args.gap_samples)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(to_cs8(x))
    meta = {"format": "cs8", "center_freq_hz": args.center_mhz * 1_000_000,
            "sample_rate_sps": args.bandwidth_mhz * 1_000_000, "seconds": x.size / (args.bandwidth_mhz * 1e6),
            "snr_db": args.snr_db, "gap_every": args.gap_every, "gap_samples": args.gap_samples,
            "laps": [f"{lap:06X}" for lap in laps], "packets": truth}
    args.out.with_suffix(".json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"{args.out}: {len(truth)} packets, {meta['seconds']:.2f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
