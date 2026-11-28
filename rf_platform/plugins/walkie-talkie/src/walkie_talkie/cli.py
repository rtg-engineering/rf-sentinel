from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

from .dsp import CaptureMetadata, classify_walkie_signal, load_capture, save_audio_wav, save_capture
from .gateway import GatewayClient


DEFAULT_CENTER_FREQ_HZ = 462_500_000
DEFAULT_SAMPLE_RATE_SPS = 1_000_000
DEFAULT_BANDWIDTH_HZ = 250_000
DEFAULT_DURATION_S = 3.0
DEFAULT_RECORDING_DIR = Path("recordings/walkie")  # relative to where the scanner runs


def _json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def _safe_stem(device_id: str, center_freq_hz: int, sample_rate_sps: int, duration_s: float) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    device_slug = str(device_id).replace(":", "_").replace("/", "_")
    duration_text = f"{duration_s:.1f}".rstrip("0").rstrip(".")
    return f"walkie_{device_slug}_{int(center_freq_hz)}_{int(sample_rate_sps)}_{duration_text}s_{stamp}"


def _capture_iq(
    client: GatewayClient,
    *,
    device_id: str,
    center_freq_hz: int,
    sample_rate_sps: int,
    duration_s: float,
    lna_gain_db: int,
    vga_gain_db: int,
    amp_enable: bool,
    baseband_filter_hz: int,
    replace_existing: bool,
) -> np.ndarray:
    stream = client.start_stream(
        device_id=device_id,
        center_freq_hz=center_freq_hz,
        sample_rate_sps=sample_rate_sps,
        lna_gain_db=lna_gain_db,
        vga_gain_db=vga_gain_db,
        amp_enable=amp_enable,
        baseband_filter_hz=baseband_filter_hz,
        replace_existing=replace_existing,
    )
    deadline = time.monotonic() + max(0.2, float(duration_s))
    chunks: list[bytes] = []
    try:
        for chunk in client.iter_iq_chunks(stream.stream_id, keep_stream=True, deadline_monotonic=deadline):
            chunks.append(bytes(chunk))
            if time.monotonic() >= deadline:
                break
    finally:
        client.stop_stream(stream.stream_id)
    if not chunks:
        return np.empty(0, dtype=np.complex64)
    from .dsp import iq_i8_to_complex

    return iq_i8_to_complex(b"".join(chunks))


def _recording_dir(explicit: Path | None) -> Path:
    return explicit or DEFAULT_RECORDING_DIR


def cmd_devices(client: GatewayClient, emit_json: bool) -> int:
    devices = client.list_devices()
    if emit_json:
        print(json.dumps(devices, indent=2, sort_keys=True))
        return 0
    if not devices:
        print("No SDR devices found.", file=sys.stderr)
        return 1
    print(f"{'ID':<14} {'DRIVER':<10} {'LABEL':<26} {'OCCUPIED':<10} NOTES")
    for device in devices:
        occupied = "yes" if bool(device.get("occupied")) else "no"
        notes = str(device.get("notes") or "")
        print(f"{str(device.get('id') or ''):<14} {str(device.get('driver') or ''):<10} {str(device.get('label') or ''):<26} {occupied:<10} {notes}")
    return 0


def cmd_capture(client: GatewayClient, args: argparse.Namespace) -> int:
    try:
        device_id = args.device_id or client.resolve_default_device_id()
        iq = _capture_iq(
            client,
            device_id=device_id,
            center_freq_hz=int(args.center_freq_hz),
            sample_rate_sps=int(args.sample_rate_sps),
            duration_s=float(args.duration_s),
            lna_gain_db=int(args.lna_gain_db),
            vga_gain_db=int(args.vga_gain_db),
            amp_enable=bool(args.amp_enable),
            baseband_filter_hz=int(args.baseband_filter_hz),
            replace_existing=bool(args.replace_existing),
        )
    except Exception as exc:
        print(f"walkie capture failed: {exc}", file=sys.stderr)
        return 1
    if iq.size == 0:
        print("walkie capture failed: no IQ received", file=sys.stderr)
        return 1
    stem = _safe_stem(device_id, int(args.center_freq_hz), int(args.sample_rate_sps), float(args.duration_s))
    metadata = CaptureMetadata(
        center_freq_hz=int(args.center_freq_hz),
        sample_rate_sps=int(args.sample_rate_sps),
        duration_s=float(args.duration_s),
        device_id=device_id,
        baseband_filter_hz=int(args.baseband_filter_hz),
        lna_gain_db=int(args.lna_gain_db),
        vga_gain_db=int(args.vga_gain_db),
        amp_enable=bool(args.amp_enable),
        captured_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        iq_path="",
    )
    iq_path, meta_path = save_capture(iq, metadata, _recording_dir(args.save_dir), stem)
    output: dict[str, Any] = {
        "protocol": "walkie",
        "kind": "capture",
        "device_id": device_id,
        "center_freq_hz": int(args.center_freq_hz),
        "sample_rate_sps": int(args.sample_rate_sps),
        "duration_s": round(float(args.duration_s), 3),
        "iq_path": str(iq_path),
        "metadata_path": str(meta_path),
        "samples": int(iq.size),
    }
    if args.classify:
        result, audio = classify_walkie_signal(iq, int(args.sample_rate_sps))
        output["classification"] = result.to_dict()
        wav_path = iq_path.with_suffix(".wav")
        save_audio_wav(audio, wav_path)
        output["wav_path"] = str(wav_path)
    if args.json:
        _json_print(output)
    else:
        print(json.dumps(output, indent=2, sort_keys=True))
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    iq_path = Path(args.iq_path)
    iq, metadata = load_capture(iq_path, Path(args.metadata_path) if args.metadata_path else None)
    sample_rate_sps = int(args.sample_rate_sps or metadata.get("sample_rate_sps") or DEFAULT_SAMPLE_RATE_SPS)
    center_freq_hz = int(metadata.get("center_freq_hz") or args.center_freq_hz or DEFAULT_CENTER_FREQ_HZ)
    result, audio = classify_walkie_signal(iq, sample_rate_sps)
    wav_path = None
    if args.wav_out:
        wav_path = save_audio_wav(audio, Path(args.wav_out))
    elif args.write_wav:
        wav_path = save_audio_wav(audio, iq_path.with_suffix(".wav"))
    output = {
        "protocol": "walkie",
        "kind": "classification",
        "iq_path": str(iq_path),
        "center_freq_hz": center_freq_hz,
        "sample_rate_sps": sample_rate_sps,
        "classification": result.to_dict(),
        "wav_path": str(wav_path) if wav_path is not None else None,
    }
    if args.json:
        _json_print(output)
    else:
        print(json.dumps(output, indent=2, sort_keys=True))
    return 0


def cmd_scan(client: GatewayClient, args: argparse.Namespace) -> int:
    try:
        device_id = args.device_id or client.resolve_default_device_id()
        iq = _capture_iq(
            client,
            device_id=device_id,
            center_freq_hz=int(args.center_freq_hz),
            sample_rate_sps=int(args.sample_rate_sps),
            duration_s=float(args.scan_window_s),
            lna_gain_db=int(args.lna_gain_db),
            vga_gain_db=int(args.vga_gain_db),
            amp_enable=bool(args.amp_enable),
            baseband_filter_hz=int(args.baseband_filter_hz),
            replace_existing=bool(args.replace_existing),
        )
    except Exception as exc:
        print(f"walkie scan failed: {exc}", file=sys.stderr)
        return 1

    result, audio = classify_walkie_signal(iq, int(args.sample_rate_sps))
    if result.label == "no_signal" and not args.emit_empty:
        return 0

    saved_iq_path = None
    saved_meta_path = None
    saved_wav_path = None
    if args.save_dir and iq.size:
        stem = _safe_stem(device_id, int(args.center_freq_hz), int(args.sample_rate_sps), float(args.scan_window_s))
        metadata = CaptureMetadata(
            center_freq_hz=int(args.center_freq_hz),
            sample_rate_sps=int(args.sample_rate_sps),
            duration_s=float(args.scan_window_s),
            device_id=device_id,
            baseband_filter_hz=int(args.baseband_filter_hz),
            lna_gain_db=int(args.lna_gain_db),
            vga_gain_db=int(args.vga_gain_db),
            amp_enable=bool(args.amp_enable),
            captured_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            iq_path="",
        )
        saved_iq_path, saved_meta_path = save_capture(iq, metadata, Path(args.save_dir), stem)
        if args.write_wav:
            saved_wav_path = save_audio_wav(audio, saved_iq_path.with_suffix(".wav"))

    event = {
        "protocol": "walkie",
        "kind": "walkie_signal",
        "seen_at": round(time.time(), 3),
        "identity": f"Walkie {float(args.center_freq_hz) / 1_000_000:.3f} MHz",
        "device_type": "Walkie-talkie",
        "device_type_detail": result.label.replace("_", " "),
        "classification": result.label,
        "modulation": result.modulation,
        "confidence": result.confidence,
        "center_freq_hz": int(args.center_freq_hz),
        "frequency_hz": int(args.center_freq_hz),
        "frequency_mhz": round(float(args.center_freq_hz) / 1_000_000.0, 4),
        "sample_rate_sps": int(args.sample_rate_sps),
        "duration_s": round(float(args.scan_window_s), 3),
        "rssi_dbfs": result.features.signal_dbfs,
        "last_rssi_dbfs": result.features.signal_dbfs,
        "signal_dbfs": result.features.signal_dbfs,
        "audio_rms_dbfs": result.features.audio_rms_dbfs,
        "audio_bandwidth_hz": result.features.audio_bandwidth_hz,
        "voice_band_ratio": result.features.voice_band_ratio,
        "voice_activity_ratio": result.features.voice_activity_ratio,
        "occupied_ratio": result.features.occupied_ratio,
        "freq_std_hz": result.features.freq_std_hz,
        "saved_iq_path": str(saved_iq_path) if saved_iq_path is not None else None,
        "saved_meta_path": str(saved_meta_path) if saved_meta_path is not None else None,
        "saved_wav_path": str(saved_wav_path) if saved_wav_path is not None else None,
    }
    if args.json:
        _json_print(event)
    else:
        print(json.dumps(event, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="walkie_talkie_scanner", description="Capture and classify narrowband walkie-talkie activity with sdr-gateway.")
    parser.add_argument("--base-url", dest="base_url", default=None, help="Gateway base URL, default comes from SDR_GATEWAY_BASE_URL")
    parser.add_argument("--token", default=None, help="Gateway API token, default comes from SDR_GATEWAY_API_TOKEN")
    subparsers = parser.add_subparsers(dest="command", required=True)

    devices = subparsers.add_parser("devices", help="List visible SDR devices")
    devices.add_argument("--json", action="store_true")

    capture = subparsers.add_parser("capture", help="Capture a short IQ recording around the walkie frequency")
    _add_capture_args(capture)
    capture.add_argument("--classify", action="store_true", help="Run the offline classifier immediately after capture")

    classify = subparsers.add_parser("classify", help="Classify a saved IQ capture and optionally render audio")
    classify.add_argument("iq_path", help="Path to the .iq capture")
    classify.add_argument("--metadata-path", default="", help="Optional JSON metadata path; defaults to sibling .json")
    classify.add_argument("--sample-rate-sps", type=int, default=0, help="Override sample rate if metadata is missing")
    classify.add_argument("--center-freq-hz", type=int, default=DEFAULT_CENTER_FREQ_HZ)
    classify.add_argument("--wav-out", default="", help="Write demodulated mono WAV to this path")
    classify.add_argument("--write-wav", action="store_true", help="Write WAV beside the input IQ capture")
    classify.add_argument("--json", action="store_true")

    scan = subparsers.add_parser("scan", help="Capture, classify, and emit one JSON event for RF Sentinel")
    _add_scan_args(scan)

    return parser


def _add_common_radio_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--device-id", default="", help="Device id from sdr-gateway /devices")
    parser.add_argument("--center-freq-hz", type=int, default=DEFAULT_CENTER_FREQ_HZ)
    parser.add_argument("--sample-rate-sps", type=int, default=DEFAULT_SAMPLE_RATE_SPS)
    parser.add_argument("--baseband-filter-hz", type=int, default=DEFAULT_BANDWIDTH_HZ)
    parser.add_argument("--lna-gain-db", type=int, default=24)
    parser.add_argument("--vga-gain-db", type=int, default=28)
    parser.add_argument("--amp-enable", action="store_true")
    parser.add_argument("--replace-existing", action="store_true", default=True)
    parser.add_argument("--no-replace-existing", dest="replace_existing", action="store_false")


def _add_capture_args(parser: argparse.ArgumentParser) -> None:
    _add_common_radio_args(parser)
    parser.add_argument("--duration-s", type=float, default=DEFAULT_DURATION_S)
    parser.add_argument("--save-dir", type=Path, default=DEFAULT_RECORDING_DIR)
    parser.add_argument("--json", action="store_true")


def _add_scan_args(parser: argparse.ArgumentParser) -> None:
    _add_common_radio_args(parser)
    parser.add_argument("--scan-window-s", type=float, default=DEFAULT_DURATION_S)
    parser.add_argument("--save-dir", type=Path, default=None)
    parser.add_argument("--write-wav", action="store_true")
    parser.add_argument("--emit-empty", action="store_true", help="Emit a JSON no-signal event instead of exiting quietly")
    parser.add_argument("--json", action="store_true")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    client = GatewayClient(base_url=args.base_url, token=args.token)
    if args.command == "devices":
        return cmd_devices(client, emit_json=args.json)
    if args.command == "capture":
        return cmd_capture(client, args)
    if args.command == "classify":
        return cmd_classify(args)
    if args.command == "scan":
        return cmd_scan(client, args)
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
