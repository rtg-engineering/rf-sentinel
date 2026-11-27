"""Discover the airwaves: sweep the receiver's whole range, find what is on the air, explain it, propose what next.

  rf-sentinel agent run "what's on the air around me?"
  rf-sentinel agent run "discover the airwaves for 2 minutes"

1. Sweep. A receive-only power sweep (the IQ service's /sweeps, 15 MHz per step) passes over 70 MHz-6 GHz again and
   again for the requested time.
2. Find. Per step, the noise floor is a low percentile of that step's bins, so a slope in the receiver's response
   does not look like a signal. Bins that stand 8 dB or more above the floor merge into segments, with peak level,
   width, and how many passes they were up in (steady carriers versus bursts).
3. Explain. Each segment is matched to the US band plan below: what is usually there, and whether RF Sentinel has a
   decoder for it. A segment outside every listed band is reported as unidentified energy. Allocations say what a
   band is *for*; they do not prove what a given signal is.
4. Propose. Bands with a decoder (2.4 GHz: BLE, Bluetooth Classic, Zigbee; 315/433 MHz: TPMS; cellular downlinks:
   the modem survey) become a follow-up request in plain language, planned and validated by the normal playbook
   planner, for the operator to run. Nothing is transmitted at any point.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
import time
from typing import Any, Callable

import numpy as np

START_HZ, STOP_HZ = 70_000_000, 6_000_000_000
THRESHOLD_DB = 8.0
MIN_S, MAX_S, DEFAULT_S = 20, 300, 120  # one full pass takes about 60 s on a bladeRF


@dataclass(frozen=True)
class Band:
    lo: int
    hi: int
    name: str
    usual: str
    decoder: str | None = None  # agent profile that can decode it


MHZ = 1_000_000
# US allocations (FCC/NTIA), the ones a receiver here is likely to see. Downlinks for cellular: what a passive
# receiver hears is the towers.
BANDS: list[Band] = [
    Band(54 * MHZ, 88 * MHZ, "VHF TV (low band)", "digital TV (ATSC) channels 2-6"),
    Band(88 * MHZ, 108 * MHZ, "FM broadcast", "FM radio stations"),
    Band(138 * MHZ, 144 * MHZ, "Federal VHF", "government and military land mobile"),
    Band(216 * MHZ, 220 * MHZ, "216-220 MHz", "telemetry, assistive listening, AMTS"),
    Band(222 * MHZ, 225 * MHZ, "1.25 m amateur", "amateur radio"),
    Band(108 * MHZ, 118 * MHZ, "Aeronautical navigation", "VOR/ILS beacons"),
    Band(118 * MHZ, 137 * MHZ, "Airband (VHF AM)", "aircraft and tower voice"),
    Band(137 * MHZ, 138 * MHZ, "Weather satellites", "NOAA/Meteor APT and LRPT downlinks"),
    Band(144 * MHZ, 148 * MHZ, "2 m amateur", "amateur radio, APRS at 144.39 MHz"),
    Band(150 * MHZ, 162 * MHZ, "VHF land mobile", "business, public-safety and rail radio"),
    Band(162_400_000, 162_550_000, "NOAA Weather Radio", "continuous weather broadcasts"),
    Band(156 * MHZ, 162_025_000, "VHF marine", "ship and harbour voice, AIS at 161.975/162.025 MHz"),
    Band(174 * MHZ, 216 * MHZ, "VHF TV", "digital TV (ATSC) channels 7-13"),
    Band(225 * MHZ, 400 * MHZ, "Military UHF", "military aviation and satellite links"),
    Band(314 * MHZ, 316 * MHZ, "315 MHz ISM", "tire-pressure sensors, car remotes, garage doors", "tpms"),
    Band(420 * MHZ, 450 * MHZ, "70 cm amateur", "amateur radio"),
    Band(433_050_000, 434_790_000, "433 MHz ISM", "weather stations, remotes, EU-style tire sensors", "tpms"),
    Band(450 * MHZ, 470 * MHZ, "UHF land mobile", "business and public-safety radio"),
    Band(462_550_000, 467_725_000, "FRS/GMRS", "walkie-talkies"),
    Band(470 * MHZ, 608 * MHZ, "UHF TV", "digital TV (ATSC) channels 14-36"),
    Band(617 * MHZ, 652 * MHZ, "Cellular 600 MHz (B71/n71) downlink", "T-Mobile LTE/5G towers", "cellular"),
    Band(729 * MHZ, 746 * MHZ, "Cellular 700 MHz (B12/17) downlink", "LTE towers", "cellular"),
    Band(746 * MHZ, 756 * MHZ, "Cellular 700 MHz (B13) downlink", "Verizon LTE towers", "cellular"),
    Band(758 * MHZ, 768 * MHZ, "FirstNet 700 MHz (B14) downlink", "public-safety LTE towers", "cellular"),
    Band(769 * MHZ, 806 * MHZ, "700 MHz public safety", "public-safety radio"),
    Band(851 * MHZ, 869 * MHZ, "800 MHz land mobile", "public-safety and trunked radio"),
    Band(869 * MHZ, 894 * MHZ, "Cellular 850 MHz (B5/n5) downlink", "LTE/5G towers", "cellular"),
    Band(902 * MHZ, 928 * MHZ, "915 MHz ISM", "LoRa, utility meters, sensors, RFID"),
    Band(960 * MHZ, 1215 * MHZ, "Aeronautical radionavigation", "DME/TACAN; ADS-B and transponders at 1090 MHz"),
    Band(1525 * MHZ, 1559 * MHZ, "L-band satellite", "Inmarsat downlinks"),
    Band(1559 * MHZ, 1610 * MHZ, "GNSS", "GPS L1 / Galileo / GLONASS (normally below the noise floor)"),
    Band(1616 * MHZ, 1626_500_000, "Iridium", "satellite phones and pagers"),
    Band(1920 * MHZ, 1930 * MHZ, "DECT 6.0", "cordless phones"),
    Band(1930 * MHZ, 1995 * MHZ, "Cellular PCS 1900 (B2/25) downlink", "LTE/5G towers", "cellular"),
    Band(2110 * MHZ, 2200 * MHZ, "Cellular AWS (B4/66) downlink", "LTE/5G towers", "cellular"),
    Band(2350 * MHZ, 2360 * MHZ, "WCS (B30) downlink", "AT&T LTE towers", "cellular"),
    Band(2400 * MHZ, 2483_500_000, "2.4 GHz ISM", "WiFi, Bluetooth, BLE, Zigbee, microwave ovens", "ble,classic,zigbee"),
    Band(2496 * MHZ, 2690 * MHZ, "Cellular 2.5 GHz (B41/n41)", "T-Mobile LTE/5G towers", "cellular"),
    Band(3550 * MHZ, 3700 * MHZ, "CBRS (B48/n48)", "private and carrier LTE/5G"),
    Band(3700 * MHZ, 3980 * MHZ, "C-band 5G (n77)", "Verizon/AT&T 5G towers", "cellular"),
    Band(5150 * MHZ, 5350 * MHZ, "5 GHz UNII-1/2", "WiFi"),
    Band(5470 * MHZ, 5725 * MHZ, "5 GHz UNII-2e", "WiFi (with radar avoidance)"),
    Band(5725 * MHZ, 5850 * MHZ, "5.8 GHz ISM / UNII-3", "WiFi, FPV video, drone links"),
    Band(5850 * MHZ, 5925 * MHZ, "5.9 GHz C-V2X", "vehicle-to-everything"),
]


def bands_for(lo: int, hi: int) -> list[Band]:
    """Bands a segment overlaps, narrowest first (the most specific explanation)."""
    hits = [b for b in BANDS if lo < b.hi and hi > b.lo]
    return sorted(hits, key=lambda b: b.hi - b.lo)


def analyse(samples: list[dict[str, Any]], threshold_db: float = THRESHOLD_DB) -> dict[str, Any]:
    """Sweep samples ({hz_low, hz_high, db_values}) -> spectrum, noise floor, occupied segments."""
    steps: dict[int, list[np.ndarray]] = {}
    edges: dict[int, int] = {}
    for s in samples:
        values = np.asarray(s.get("db_values") or [], dtype=float)
        if values.size < 4:
            continue
        # The receiver's DC offset puts a spike at the centre of every tuning step (seen live on bladeRF): replace
        # the centre bins with their neighbours' median so it is never reported as a signal.
        mid = values.size // 2
        values = values.copy()
        values[max(0, mid - 1):mid + 2] = np.median(np.r_[values[max(0, mid - 6):max(0, mid - 1)], values[mid + 2:mid + 7]])
        steps.setdefault(int(s["hz_low"]), []).append(values)
        edges[int(s["hz_low"])] = int(s["hz_high"])
    freqs, mean_db, peak_db, floor_db, up_frac = [], [], [], [], []
    order = sorted(steps)
    stacks = {lo: np.vstack([p for p in steps[lo] if p.size == steps[lo][0].size]) for lo in order}
    own = [float(np.percentile(stacks[lo], 20)) for lo in order]
    # A step's floor is the lower of its own and the median of its neighbourhood (+/-4 steps, about +/-60 MHz). A
    # carrier wider than one step (C-band 5G, several WiFi channels) fills its own steps and would lift their floor;
    # the neighbourhood median stays down. A minimum would not do: the receiver's sensitivity ripples by 10 dB or
    # more across hundreds of MHz (seen live on bladeRF), and a minimum turned every ripple into a "signal".
    floors = {lo: min(own[k], float(np.median(own[max(0, k - 4):k + 5]))) for k, lo in enumerate(order)}
    for lo in order:
        stack = stacks[lo]
        floor = floors[lo]
        width = steps[lo][0].size
        f = np.linspace(lo, edges[lo], width)
        freqs.append(f)
        mean_db.append(stack.mean(axis=0))
        peak_db.append(stack.max(axis=0))
        floor_db.append(np.full(width, floor))
        up_frac.append((stack > floor + threshold_db).mean(axis=0))
    if not freqs:
        return {"passes": 0, "segments": [], "spectrum": [], "floor_db": None}
    f, mean, peak, floor, up = (np.concatenate(x) for x in (freqs, mean_db, peak_db, floor_db, up_frac))
    busy = (peak - floor >= threshold_db) & (up > 0)
    segments: list[dict[str, Any]] = []
    i, n = 0, len(f)
    gap = max(1, int(round(200_000 / max(1.0, float(np.median(np.diff(f))) or 1.0))))  # bridge gaps under ~200 kHz
    while i < n:
        if not busy[i]:
            i += 1
            continue
        j, last = i, i
        while j < n and (busy[j] or j - last <= gap):
            if busy[j]:
                last = j
            j += 1
        lo_i, hi_i = i, last
        seg_peak = int(np.argmax(peak[lo_i:hi_i + 1])) + lo_i
        lo_hz, hi_hz = int(f[lo_i]), int(f[hi_i])
        # Named by where its peak is (narrowest band first); only a segment whose peak is in no band falls back to
        # any band it overlaps.
        matched = bands_for(int(f[seg_peak]), int(f[seg_peak]) + 1) or bands_for(lo_hz, max(hi_hz, lo_hz + 1))
        best = matched[0] if matched else None
        segments.append({
            "lo_hz": lo_hz, "hi_hz": hi_hz, "width_hz": max(0, hi_hz - lo_hz),
            "peak_hz": int(f[seg_peak]), "peak_db": round(float(peak[seg_peak]), 1),
            "above_floor_db": round(float(peak[seg_peak] - floor[seg_peak]), 1),
            "up_fraction": round(float(up[lo_i:hi_i + 1].max()), 2),
            "band": best.name if best else None, "usual": best.usual if best else None,
            "decoder": best.decoder if best else None, "also": [b.name for b in matched[1:3]]})
        i = j
    segments.sort(key=lambda s: -s["above_floor_db"])
    # A light spectrum for display: at most 1500 points, max-hold within each bucket.
    stride = max(1, math.ceil(n / 1500))
    spectrum = [[int(f[k]), round(float(mean[k:k + stride].max()), 1), round(float(peak[k:k + stride].max()), 1),
                 round(float(floor[k]), 1)] for k in range(0, n, stride)]
    passes = min(len(v) for v in steps.values())
    return {"passes": passes, "segments": segments, "spectrum": spectrum,
            "floor_db": round(float(np.median(floor)), 1), "threshold_db": threshold_db,
            "range_hz": [int(f[0]), int(f[-1])]}


def summarise_bands(segments: list[dict[str, Any]], passes: int = 2) -> list[dict[str, Any]]:
    """Segments grouped by band: one line per kind of activity. With one pass, steady/intermittent is unknown (None)."""
    out: dict[str, dict[str, Any]] = {}
    for s in segments:
        name = s["band"] or f"Unidentified near {s['peak_hz'] / 1e6:,.0f} MHz"
        item = out.setdefault(name, {"band": name, "usual": s["usual"], "decoder": s["decoder"], "segments": 0,
                                     "occupied_hz": 0, "peak_db": s["peak_db"], "above_floor_db": s["above_floor_db"],
                                     "lo_hz": s["lo_hz"], "hi_hz": s["hi_hz"], "steady": False})
        item["segments"] += 1
        item["occupied_hz"] += s["width_hz"]
        item["above_floor_db"] = max(item["above_floor_db"], s["above_floor_db"])
        item["peak_db"] = max(item["peak_db"], s["peak_db"])
        item["lo_hz"], item["hi_hz"] = min(item["lo_hz"], s["lo_hz"]), max(item["hi_hz"], s["hi_hz"])
        item["steady"] = None if passes < 2 else (item["steady"] or s["up_fraction"] >= 0.8)
    return sorted(out.values(), key=lambda b: -b["above_floor_db"])


NAMES = {"ble": "Bluetooth LE", "classic": "Bluetooth Classic", "zigbee": "Zigbee", "tpms": "Tire-pressure sensors",
         "cellular": "Cell-tower survey"}
FOLLOW_UP = {  # decoder profile -> the plain-language step the planner turns into a validated workflow
    "ble": "find bluetooth low energy devices for 30 seconds",
    "classic": "find bluetooth classic devices for 30 seconds",
    "zigbee": "listen for zigbee for 30 seconds",
    "tpms": "find tire sensors for 30 seconds",
    "cellular": "survey cell towers for 90 seconds",
}


def propose(bands: list[dict[str, Any]]) -> tuple[str | None, list[str]]:
    """The follow-up request for bands RF Sentinel can decode, and why each step is there."""
    wanted: list[str] = []
    why: list[str] = []
    for b in bands:
        for profile in (b["decoder"] or "").split(","):
            if profile and profile not in wanted:
                wanted.append(profile)
                why.append(f"{NAMES[profile]}: energy in {b['band']} "
                           f"({b['above_floor_db']:.0f} dB above the floor)")
    order = [p for p in ("ble", "classic", "zigbee", "tpms", "cellular") if p in wanted]
    if not order:
        return None, []
    steps = [FOLLOW_UP[p] for p in order]
    text = ". Then ".join(s[0].upper() + s[1:] if i == 0 else s for i, s in enumerate(steps)) + "."
    return text, why


def narrative(result: dict[str, Any]) -> list[str]:
    a = result["analysis"]
    mhz = lambda hz: f"{hz / 1e6:,.0f} MHz" if hz < 1e9 else f"{hz / 1e9:.2f} GHz"  # noqa: E731
    one_pass = a["passes"] == 1
    lines = [f"{'Simulated sweep' if result.get('simulated') else 'Swept'} {mhz(a['range_hz'][0])}–{mhz(a['range_hz'][1])} "
             f"{a['passes']} time(s) in {result['elapsed_s']:.0f} s on {result['receiver']}; noise floor about "
             f"{a['floor_db']} dB; {len(a['segments'])} active segment(s) in {len(result['bands'])} band(s)."
             + (" One pass only: whether signals are steady or intermittent is not judged." if one_pass else "")
             if a["passes"] else "No complete sweep was collected."]
    for b in result["bands"][:6]:
        lines.append(f"{b['band']}: {mhz(b['lo_hz'])}–{mhz(b['hi_hz'])}, up to {b['above_floor_db']:.0f} dB above the floor"
                     + ("" if b["steady"] is None else ", steady" if b["steady"] else ", intermittent")
                     + (f" (usually {b['usual']})" if b["usual"] else "")
                     + ("; RF Sentinel can follow up on it" if b["decoder"] else "; no decoder here, energy only"))
    if len(result["bands"]) > 6:
        lines.append(f"…and {len(result['bands']) - 6} more band(s).")
    if result.get("proposal"):
        lines.append("Proposed next: " + result["proposal"])
    return lines


def discover(radio, gateway, duration_s: int = DEFAULT_S, start_hz: int = START_HZ, stop_hz: int = STOP_HZ,
             on_step: Callable[[str], None] | None = None, on_event: Callable[[dict[str, Any]], None] | None = None,
             should_stop: Callable[[], bool] | None = None, simulated: bool = False) -> dict[str, Any]:
    """Run the sweep, analyse it, and propose a follow-up. radio: agent Radio or SimRadio (both have sweeps)."""
    step = on_step or (lambda m: None)
    emit = on_event or (lambda e: None)
    stop = should_stop or (lambda: False)
    duration_s = int(max(MIN_S, min(MAX_S, duration_s)))
    device = radio.choose((max(start_hz, 70_000_000), stop_hz), 20_000_000)
    start_hz = max(start_hz, int(device.get("freq_min_hz", start_hz)))
    stop_hz = min(stop_hz, int(device.get("freq_max_hz", stop_hz)))
    gateway.authorize("spectrum_sweep", {"device_id": device["id"], "start_hz": start_hz, "stop_hz": stop_hz,
                                         "duration_s": duration_s})
    step(f"Receiver: {device['id']} ({device.get('label', '')}); sweeping {start_hz / 1e6:.0f}-{stop_hz / 1e6:.0f} MHz "
         f"for {duration_s} s (receive-only)")
    started = time.time()
    sweep_id = radio.start_sweep(device, start_hz, stop_hz)
    samples: dict[tuple[str, int], dict[str, Any]] = {}
    last_report = 0.0
    try:
        while time.time() - started < duration_s and not stop():
            time.sleep(1.0)
            for s in radio.sweep_samples(sweep_id):
                samples[(s.get("timestamp", ""), int(s["hz_low"]))] = s
            done = len({k[1] for k in samples})
            if time.time() - last_report >= 5:
                last_report = time.time()
                partial = analyse(list(samples.values()))
                emit({"kind": "sweep", "t": round(time.time() - started, 1), "steps_seen": done,
                      "passes": partial["passes"], "segments": len(partial["segments"]),
                      "spectrum": partial["spectrum"],
                      "top": [{k: s[k] for k in ("peak_hz", "above_floor_db", "band")} for s in partial["segments"][:8]]})
                step(f"{partial['passes']} full pass(es), {len(partial['segments'])} active segment(s) so far")
    finally:
        radio.stop_sweep(sweep_id)
    analysis = analyse(list(samples.values()))
    bands = summarise_bands(analysis["segments"], analysis["passes"])
    proposal, why = propose(bands)
    result = {"kind": "discovery", "receiver": device["id"], "simulated": simulated, "started": started,
              "elapsed_s": round(time.time() - started, 1), "analysis": analysis, "bands": bands,
              "proposal": proposal, "proposal_reasons": why, "stopped": stop()}
    result["narrative"] = narrative(result)
    for line in result["narrative"]:
        step(line)
    return result


def simulated_samples(start_hz: int, stop_hz: int, pass_index: int, seed: int = 7) -> list[dict[str, Any]]:
    """One synthetic sweep pass with a scripted scene: FM stations, TV, cell downlinks, 2.4 GHz traffic, a 433 MHz
    burst every few passes. Clearly labelled SIMULATED wherever it is shown."""
    rng = np.random.default_rng(seed * 1000 + pass_index)
    usable, bin_hz = 15_000_000, 100_000
    out = []
    scene = [(88.5e6, 0.2e6, 30), (94.7e6, 0.2e6, 34), (101.1e6, 0.2e6, 28), (162.475e6, 0.02e6, 12),
             (521e6, 6e6, 18), (737.5e6, 10e6, 22), (751e6, 10e6, 25), (881.5e6, 10e6, 24), (1960e6, 15e6, 20),
             (2132.5e6, 20e6, 23), (2437e6, 20e6, 26), (2462e6, 20e6, 15), (2595e6, 20e6, 19), (3750e6, 40e6, 14)]
    if pass_index % 3 == 0:
        scene.append((433.92e6, 0.1e6, 20))
    if pass_index % 2 == 0:
        scene.append((315.0e6, 0.1e6, 16))
    lo = start_hz
    while lo < stop_hz:
        hi = min(lo + usable, stop_hz)
        f = np.arange(lo, hi, bin_hz)
        floor = -95 + 3 * np.sin(lo / 7e8)
        db = floor + rng.normal(0, 1.0, f.size)
        for center, width, level in scene:
            inside = np.abs(f - center) <= max(width / 2, bin_hz / 2)
            db[inside] = np.maximum(db[inside], floor + level + rng.normal(0, 1.0, inside.sum()))
        out.append({"timestamp": f"sim-{pass_index}-{lo}", "hz_low": int(lo), "hz_high": int(f[-1]),
                    "db_values": [round(float(v), 2) for v in db]})
        lo = hi
    return out


DISCOVERY = re.compile(
    r"\b(?:discover|explore|map|survey|sweep|scan|check)\b[^.]*\b(?:airwaves?|spectrum|everything|all (?:the )?bands|"
    r"the air|whole range|full range)\b|\bwhat(?:'s| is| else is)? (?:on the air|out there|in the air|transmitting|"
    r"around me|around here)\b|\bwhat(?:'s| is) (?:the )?(?:rf|radio) (?:environment|picture|scene)\b",
    re.I)


def is_discovery(text: str) -> bool:
    return bool(DISCOVERY.search(text or ""))


def plan_discovery(text: str) -> dict[str, Any]:
    """{"discovery": {...}, "summary", "explain", "planner"} for "what's on the air?"-style requests."""
    from .workflow import UNIT_S, _seconds
    m = re.search(rf"\bfor (?:about |around )?(\d+(?:\.\d+)?)\s*({UNIT_S})", text, re.I)
    duration = int(_seconds(m.group(1), m.group(2))) if m else DEFAULT_S
    if not MIN_S <= duration <= MAX_S:
        raise ValueError(f"A discovery sweep runs {MIN_S}-{MAX_S} s; asked for {duration} s.")
    spec = {"duration_s": duration, "start_hz": START_HZ, "stop_hz": STOP_HZ}
    return {"discovery": spec, "planner": "deterministic", "model": None, "fallback_reason": None,
            "summary": {"find": f"Everything a receiver hears from 70 MHz to 6 GHz, for {duration} s"},
            "explain": explain_discovery(spec)}


def explain_discovery(spec: dict[str, Any]) -> str:
    from .explain import _duration
    return (f"I'll sweep everything from {spec['start_hz'] / 1e6:.0f} MHz to {spec['stop_hz'] / 1e9:.0f} GHz again and "
            f"again for {_duration(spec['duration_s'])}, find what stands above the noise, name each band from the US "
            "band plan, and propose a follow-up for the ones RF Sentinel can decode. Receive-only.")


def render(result: dict[str, Any]) -> str:
    """The discovery report: spectrum, bands, segments, proposal."""
    import html
    e = lambda v: html.escape("" if v is None else str(v))  # noqa: E731
    a = result["analysis"]
    spectrum = a.get("spectrum") or []
    svg = ""
    if spectrum:
        w, h = 960, 220
        lo, hi = spectrum[0][0], spectrum[-1][0]
        vals = [p[2] for p in spectrum]
        top, bottom = max(vals) + 3, min(p[3] for p in spectrum) - 6
        x = lambda f: (math.log10(f) - math.log10(lo)) / max(1e-9, math.log10(hi) - math.log10(lo)) * w  # noqa: E731
        y = lambda d: h - (d - bottom) / max(1e-9, top - bottom) * h  # noqa: E731
        peak = " ".join(f"{x(p[0]):.1f},{y(p[2]):.1f}" for p in spectrum)
        floor = " ".join(f"{x(p[0]):.1f},{y(p[3]):.1f}" for p in spectrum)
        ticks = "".join(f"<text x='{x(t):.0f}' y='{h + 14}' font-size='10' text-anchor='middle'>{lab}</text>"
                        for t, lab in ((1e8, "100 MHz"), (3e8, "300 MHz"), (1e9, "1 GHz"), (2.4e9, "2.4 GHz"), (5e9, "5 GHz"))
                        if lo <= t <= hi)
        svg = (f"<svg viewBox='0 -4 {w} {h + 22}' width='100%' role='img' aria-label='Spectrum, peak hold and noise floor'>"
               f"<polyline points='{floor}' fill='none' stroke='#8a96a3' stroke-dasharray='3 3' stroke-width='1'/>"
               f"<polyline points='{peak}' fill='none' stroke='#7e3ff2' stroke-width='1.4'/>{ticks}</svg>")
    bands = "".join(f"<tr><td>{e(b['band'])}</td><td>{b['lo_hz'] / 1e6:,.1f}–{b['hi_hz'] / 1e6:,.1f} MHz</td>"
                    f"<td>{b['above_floor_db']:.0f} dB</td><td>{'one pass' if b['steady'] is None else 'steady' if b['steady'] else 'intermittent'}</td>"
                    f"<td>{e(b['usual'] or '')}</td><td>{'decoder: ' + e(b['decoder']) if b['decoder'] else 'energy only'}</td></tr>"
                    for b in result["bands"])
    story = "".join(f"<li>{e(x)}</li>" for x in result.get("narrative") or [])
    why = "".join(f"<li>{e(x)}</li>" for x in result.get("proposal_reasons") or [])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RF Sentinel Discovery</title><style>
:root{{--bg:#f6f8fa;--ink:#17212b;--muted:#5b6b78;--line:#d8e0e6;--panel:#fff}}
@media (prefers-color-scheme: dark){{:root{{--bg:#0f1a22;--ink:#e7eef3;--muted:#9fb1bd;--line:#2a3b47;--panel:#16232d}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,sans-serif}}main{{max-width:1000px;margin:0 auto;padding:24px 16px}}
.muted{{color:var(--muted)}}.card{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:12px 0;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}}
svg text{{fill:var(--muted)}}</style></head><body><main>
{"<div class='card' style='border-color:#b77b17'><strong>SIMULATED DISCOVERY.</strong> No radio was used; the spectrum came from a scripted scene.</div>" if result.get("simulated") else ""}
<div class="muted">RF SENTINEL AGENT · RECEIVE-ONLY DISCOVERY</div><h1>What is on the air</h1>
<div class="muted">Run {e(result.get('run_id'))} · {e(result.get('started_at'))} · receiver {e(result['receiver'])} · {e(a.get('passes'))} pass(es) in {e(result['elapsed_s'])} s</div>
<div class="card">{svg}<div class="muted">Peak hold (purple) and estimated noise floor (dashed), log frequency axis.</div></div>
<h2>What happened</h2><div class="card"><ul>{story}</ul></div>
<h2>Bands</h2><div class="card"><table><tr><th>Band</th><th>Seen</th><th>Above floor</th><th>Activity</th><th>Usually</th><th>RF Sentinel</th></tr>{bands}</table>
<p class="muted">Allocations say what a band is for; they do not prove what a given signal is.</p></div>
{f"<h2>Proposed next</h2><div class='card'><p><strong>{e(result['proposal'])}</strong></p><ul>{why}</ul></div>" if result.get("proposal") else ""}
<h2>Limitations</h2><ul>{''.join(f'<li>{e(x)}</li>' for x in result.get('limitations') or [])}</ul></main></body></html>"""


def save(result: dict[str, Any], directory) -> Any:
    import hashlib
    import json
    from pathlib import Path
    from .report import finalize
    result.setdefault("command", "discover the airwaves")
    finalize(result, result.get("started", time.time()))
    result["limitations"] = [
        "Receive-only: no radio transmitted during this run.",
        "A power sweep sees energy, not identities. Bands are named from the US allocation table; a signal in a band "
        "is not proven to be the service the band is allocated to.",
        "Each 15 MHz step is observed briefly per pass: short bursts between visits are missed, and a signal is "
        "'intermittent' if it was above the floor in under 80% of passes.",
        "Levels are uncalibrated dB relative to full scale at a fixed gain; compare them within this sweep only.",
    ]
    folder = Path(directory) / result["run_id"]
    folder.mkdir(parents=True, exist_ok=True)
    files = {"run.json": json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", "report.html": render(result)}
    (folder / "checksums.sha256").write_text("".join(
        f"{hashlib.sha256(text.encode()).hexdigest()}  {name}\n" for name, text in files.items()))
    for name, text in files.items():
        (folder / name).write_text(text)
    return folder
