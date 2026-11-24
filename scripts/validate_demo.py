#!/usr/bin/env python3
"""Validate finite synthetic replay through the real Docker demo and status API."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import html
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.generate_demo_events import build_validation_events

LIMITATIONS = ("This result measures deterministic synthetic replay through the RF Sentinel demo backend and API path. "
               "It does not measure live RF decoder accuracy, receiver sensitivity, protocol coverage in an uncontrolled "
               "environment, or production deployment performance. API polling latency includes observer delay; it is not browser paint latency.")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def percentiles(samples):
    """Nearest rank: sorted[ceil(p*N)-1], including all observations."""
    values = sorted(samples)
    result = {"samples": len(values), "minimum": None, "p50": None, "p95": None, "p99": None, "maximum": None}
    if values:
        result.update(minimum=values[0], maximum=values[-1])
        for name, p in (("p50", .50), ("p95", .95), ("p99", .99)):
            result[name] = values[math.ceil(p * len(values)) - 1]
    return result


def latency_trend(samples):
    """Describe drift without inventing an unapproved performance threshold."""
    window = max(1, len(samples) // 3)
    first = percentiles(samples[:window])
    last = percentiles(samples[-window:])
    return {'window': 'first and last third of accepted events',
            'first': first, 'last': last,
            'p95_delta_ms': last['p95'] - first['p95'] if samples else None}


def accounting(metrics):
    return {**{key: metrics['events_' + key] for key in ('attempted', 'accepted', 'rejected', 'dropped')},
            "rejection_reasons": metrics['rejection_reasons']}


def reconciles(counts):
    return (all(isinstance(counts[k], int) and counts[k] >= 0 for k in ('attempted', 'accepted', 'rejected', 'dropped'))
            and counts['attempted'] == counts['accepted'] + counts['rejected'] + counts['dropped']
            and sum(counts['rejection_reasons'].values()) == counts['rejected'])


def stable_result(run):
    return {key: run[key] for key in ('conclusions', 'event_accounting', 'scenario_checks')}


def compare_runs(runs):
    stable = [stable_result(run) for run in runs]
    def same(key):
        return bool(runs) and all(run['conclusions'][key] == runs[0]['conclusions'][key] for run in runs)
    return {"run_count": len(runs), "protocol_counts_match": same('protocol_counts'),
            "entity_counts_match": same('entity_count'), "pattern_conclusions_match": same('patterns'),
            "all_stable_results_match": bool(stable) and all(item == stable[0] for item in stable),
            "normalized_result_digest": digest(stable[0]) if stable else None,
            "run_digests": [digest(item) for item in stable]}


def empty_resources(note="No samples collected"):
    return dict(starting_rss_bytes=None, peak_rss_bytes=None, peak_cpu_percent=None,
                log_bytes=0, restart_count=None, notes=[note])


def new_result(version, fixture, configuration):
    return dict(schema_version="1.0", application="RF Sentinel", application_version=version,
                started_at=utc_now(), completed_at=None, duration_s=0, public_safe=True,
                synthetic_replay=True, fixture=fixture, configuration=configuration,
                configuration_sha256=digest(configuration), event_accounting=dict(attempted=0, accepted=0,
                rejected=0, dropped=0, rejection_reasons={}), latency_ms=percentiles([]),
                recovery=dict(interruption_injected=False, interruption_detected=False, recovered=False,
                              recovery_time_ms=None, post_recovery_events_accepted=0),
                repeatability=compare_runs([]), resources=empty_resources(),
                conclusions=dict(protocol_counts={}, entity_count=0, wifi_ap_count=0, wifi_station_count=0,
                                 recurring_pattern_count=0, weekend_pattern_count=0, anomaly_count=0,
                                 patterns=[], topology=[]),
                checks=[], overall_status="fail", limitations=LIMITATIONS)


def result_card(result):
    """Every displayed measurement is read from the result object."""
    def value(item):
        if isinstance(item, bool):
            return 'Yes' if item else 'No'
        if isinstance(item, float):
            return f'{item:.2f}'
        return html.escape(str(item if item is not None else 'unavailable'))
    counts = result['event_accounting']; latency = result['latency_ms']; recovery = result['recovery']
    items = [('Status', result['overall_status'].upper()), ('Commit / source', result['application_version']),
             ('Fixture events / protocols', f"{result['fixture']['event_count']} / {len(result['fixture']['protocols'])}"),
             ('Accepted / rejected / dropped (all runs)', f"{counts['accepted']} / {counts['rejected']} / {counts['dropped']}"),
             ('P50 visibility latency (ms)', latency['p50']), ('P95 visibility latency (ms)', latency['p95']),
             ('Recovered', recovery['recovered']), ('Slowest recovery (ms)', recovery['recovery_time_ms']),
             ('Repeated runs match', result['repeatability']['all_stable_results_match']),
             ('Run count', result['repeatability']['run_count'])]
    restart = result.get('restart_recovery') or {}
    soak = result.get('soak') or {}
    items += [('Crash restart rebuild matches', restart.get('rebuilt_matches_uninterrupted')),
              ('Kill to API healthy (ms)', restart.get('kill_to_health_ms')),
              ('Max P95 drift, first→last third (ms)', soak.get('max_p95_delta_ms')),
              ('Max RSS growth (MiB)', None if soak.get('max_rss_growth_bytes') is None else soak['max_rss_growth_bytes'] / 2**20),
              ('Total validation time (s)', round(result['duration_s'], 2))]
    return '''<!doctype html><meta charset="utf-8"><title>RF Sentinel validation</title>
<style>body{margin:0;background:#10202d;color:#edf5f9;font:20px system-ui}main{padding:48px;width:1000px}
h1{font-size:42px;margin:8px 0}small{color:#79e6c4}dl{display:grid;grid-template-columns:1fr 1fr;gap:16px}
dt{color:#aabfcd}dd{margin:0;overflow-wrap:anywhere}p{font-size:16px;line-height:1.6;color:#aabfcd}</style>
<main><small>SYNTHETIC · PUBLIC-SAFE REPLAY</small><h1>RF Sentinel validation</h1><dl>''' + ''.join(
        f'<dt>{value(label)}</dt><dd>{value(item)}</dd>' for label, item in items) + '</dl><p>' + html.escape(result['limitations']) + '</p></main>'


def soak_summary(runs):
    """Observed drift only; no pass threshold has been approved for these values."""
    deltas = [r['latency_trend']['p95_delta_ms'] for r in runs if r['latency_trend']['p95_delta_ms'] is not None]
    growth = [r['resources']['rss_growth_bytes'] for r in runs if r['resources'].get('rss_growth_bytes') is not None]
    return dict(max_p95_delta_ms=max(deltas) if deltas else None,
                max_rss_growth_bytes=max(growth) if growth else None,
                note='First-third versus last-third P95 latency and start-to-end sampled RSS, per run; reported, not thresholded.')


def command(args, **kwargs):
    kwargs.setdefault('timeout', 1200)
    return subprocess.run(args, check=True, text=True, **kwargs)


def api(base, path='/api/status', payload=None, timeout=3):
    """POST when payload is given; path may carry a query string."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base + path, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def wait_for_status(base, timeout=60):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = api(base)
            if 'validation_metrics' in result:
                return result
        except (OSError, ValueError) as exc:
            last = exc
        time.sleep(.1)
    raise RuntimeError(f'Backend health timeout: {last}')


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def browser(args, output, name, network=None):
    docker = ['docker', 'run', '--rm', '--name', name]
    if network:
        docker += ['--network', network]
    docker += ['-v', f'{output}:/results', 'rf-sentinel:validation', 'scripts/validation_browser.py', *args]
    try:
        return command(docker, capture_output=True, timeout=90).stdout
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f'Browser validation failed: {exc.stderr[-3000:]}') from exc


def summarize(rows):
    return {"protocol_counts": dict(sorted(Counter(row['protocol'] for row in rows).items())),
            "entity_count": len(rows), "wifi_ap_count": sum(row.get('wifi_role') == 'ap' for row in rows),
            "wifi_station_count": sum(row.get('wifi_role') == 'station' for row in rows),
            "topology": sorted((row['key'], row.get('bssid', '')) for row in rows if row.get('wifi_role') == 'station')}


def scenario_checks(baseline, final):
    beacon = '02:00:00:00:99:01'; station = '72:94:11:3B:0C:90'; mesh = '02:10:7A:00:20:06'
    def find(rows, mac):
        return next((row for row in rows if row.get('mac') == mac or row.get('source_address') == mac), {})
    return {
        'baseline_recurring': any(row.get('seen_weekly_cadence') for row in baseline),
        'new_entity_after_baseline': not find(baseline, beacon) and bool(find(final, beacon)),
        'relationship_changed': find(baseline, station).get('bssid') == '02:10:7A:00:30:11' and find(final, station).get('bssid') == '02:10:7A:00:10:01',
        'strong_signal_context': float(find(final, beacon).get('last_rssi_dbfs', -120)) > -42,
        'five_station_mesh': sum(row.get('wifi_role') == 'station' and row.get('bssid') == mesh for row in final) >= 5,
        'weekly_231_days': any(row.get('seen_weekly_cadence') and row.get('seen_span_days') == 231 for row in final),
        'weekend_recurrence': any(row.get('seen_weekend_only') and row.get('seen_weekly_cadence') for row in final),
    }


def sample_resources(container):
    # Linux /proc reports the application process, not an estimate from image size.
    raw = command(['docker', 'exec', container, 'python3', '-c',
        'import json,os; s=open("/proc/1/stat").read().rsplit(")",1)[1].split(); '
        'print(json.dumps([int(s[21])*os.sysconf("SC_PAGE_SIZE"),(int(s[11])+int(s[12]))/os.sysconf("SC_CLK_TCK")]))'],
        capture_output=True).stdout
    return json.loads(raw)


def kill_and_restart(container, base):
    """SIGKILL the backend process, confirm it is gone, then start the same container again."""
    killed = time.monotonic()
    command(['docker', 'kill', '--signal', 'KILL', container], capture_output=True)
    command(['docker', 'wait', container], capture_output=True, timeout=30)
    exit_code = int(command(['docker', 'inspect', '-f', '{{.State.ExitCode}}', container], capture_output=True).stdout)
    try:
        api(base, timeout=1); unreachable = False
    except (OSError, ValueError):
        unreachable = True
    command(['docker', 'start', container], capture_output=True)
    # Docker may assign a new ephemeral host port on start.
    port = command(['docker', 'port', container, '8080/tcp'], capture_output=True).stdout.strip().rsplit(':', 1)[1]
    base = f'http://127.0.0.1:{port}'
    fresh = wait_for_status(base)
    return base, fresh, dict(exit_code=exit_code, api_unreachable_after_kill=unreachable,
                             kill_to_health_ms=(time.monotonic() - killed) * 1000)


def run_once(number, fixture_path, events, config, output, prefix, fault='pause'):
    container = f'{prefix}-{number}'
    log_path = output / 'logs' / f'run-{number}.log'
    resources = empty_resources(); observed = {}; baseline = []
    resources['samples'] = []
    recovery = dict(interruption_injected=False, interruption_detected=False, recovered=False,
                    recovery_time_ms=None, post_recovery_events_accepted=0)
    started = time.monotonic(); last_resource = 0; paused_count = None
    cursor = 0  # highest accepted_index already observed; keeps status responses bounded
    try:
        command(['docker', 'run', '-d', '--name', container, '-p', '127.0.0.1::8080',
                 '-e', 'RF_SENTINEL_DEMO_LOOP=0', '-e', 'RF_SENTINEL_DEMO_START_PAUSED=1',
                 '-e', 'RF_SENTINEL_DEMO_REFERENCE_TIMESTAMP=1795348800',
                 '-v', f'{fixture_path}:/app/.demo/events/public-demo-events.jsonl:ro',
                 'rf-sentinel:public-demo'], capture_output=True)
        port = command(['docker', 'port', container, '8080/tcp'], capture_output=True).stdout.strip().rsplit(':', 1)[1]
        base = f'http://127.0.0.1:{port}'
        status = wait_for_status(base)
        assert status['validation_metrics']['events_attempted'] == 0, 'Replay started before measurement'
        assert api(base, '/api/devices'), 'No demo devices'
        try:
            rss, cpu = sample_resources(container)
            resources.update(starting_rss_bytes=rss, peak_rss_bytes=rss, peak_cpu_percent=0, notes=[])
            previous_sample = (time.monotonic(), cpu)
            resources['samples'].append({'elapsed_s': time.monotonic()-started, 'rss_bytes': rss, 'cpu_percent': None})
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            resources['notes'] = [f'Linux process sampling unavailable: {type(exc).__name__}']
            previous_sample = None
        api(base, '/api/scan/start', {'mode': 'sentinel', 'preserve_detections': True})
        deadline = time.monotonic() + max(60, events[-1]['offset_s'] * 2 + 30)
        while time.monotonic() < deadline:
            status = api(base, f'/api/status?receipts_after={cursor}')
            seen_at = time.time()
            metrics = status['validation_metrics']
            for receipt in metrics['receipts']:
                event_id = receipt['event_id']
                cursor = max(cursor, receipt['accepted_index'])
                if event_id not in observed:
                    observed[event_id] = (seen_at - receipt['emitted_at']) * 1000
            count = metrics['events_accepted']
            if count <= len(events) - 4:
                baseline = status['discovery_table']
            if fault == 'restart' and count >= max(21, len(events) // 2) and not recovery['interruption_injected']:
                recovery.update(fault='backend_sigkill_container_restart', interrupted_at=utc_now(),
                                pre_kill_events_accepted=count, pre_kill_entity_count=len(status['discovery_table']),
                                interruption_injected=True)
                base, fresh, restart = kill_and_restart(container, base)
                recovery.update(restart)
                fresh_metrics = fresh['validation_metrics']
                # Detection state is in memory only; a clean restart must hold no partial state.
                recovery['state_after_restart'] = dict(events_attempted=fresh_metrics['events_attempted'],
                    events_accepted=fresh_metrics['events_accepted'], entity_count=len(fresh['discovery_table']))
                recovery['post_restart_state_clean'] = (fresh_metrics['events_attempted'] == 0 and
                    fresh_metrics['events_accepted'] == 0 and not fresh['discovery_table'])
                recovery['interruption_detected'] = restart['api_unreachable_after_kill'] and restart['exit_code'] == 137
                observed.clear(); baseline = []; paused_count = 0; cursor = 0
                if previous_sample:
                    try:
                        rss, cpu = sample_resources(container); previous_sample = (time.monotonic(), cpu)
                        resources['starting_rss_bytes'] = rss
                        resources['notes'].append('Restart run: starting RSS re-measured in the restarted process')
                    except (OSError, subprocess.SubprocessError, ValueError):
                        resources['notes'].append('A process sample failed'); previous_sample = None
                recovery['restored_at'] = utc_now(); recovery_start = time.monotonic()
                api(base, '/api/scan/start', {'mode': 'sentinel', 'preserve_detections': True})
                continue
            if fault == 'pause' and count >= 21 and not recovery['interruption_injected']:
                recovery['fault'] = 'replay_worker_pause_resume'
                recovery['interrupted_at'] = utc_now()
                api(base, '/api/scan/stop', {})
                stopped = api(base); paused_count = stopped['validation_metrics']['events_accepted']
                recovery['interruption_injected'] = True
                time.sleep(.3)
                still = api(base)
                recovery['interruption_detected'] = (not stopped['running'] and not still['worker_alive'] and
                    still['validation_metrics']['events_accepted'] == paused_count)
                recovery['restored_at'] = utc_now(); recovery_start = time.monotonic()
                api(base, '/api/scan/start', {'mode': 'sentinel', 'preserve_detections': True})
            if paused_count is not None and count > paused_count and not recovery['recovered']:
                recovery.update(recovered=True, recovery_time_ms=(time.monotonic() - recovery_start) * 1000,
                                recovered_at=utc_now())
            if previous_sample and time.monotonic() - last_resource >= 1:
                try:
                    rss, cpu = sample_resources(container); tick = time.monotonic()
                    resources['peak_rss_bytes'] = max(resources['peak_rss_bytes'], rss)
                    cpu_percent = (cpu - previous_sample[1]) / (tick - previous_sample[0]) * 100
                    resources['peak_cpu_percent'] = max(resources['peak_cpu_percent'], cpu_percent)
                    resources['samples'].append({'elapsed_s': tick-started, 'rss_bytes': rss, 'cpu_percent': cpu_percent})
                    previous_sample = (tick, cpu); last_resource = tick
                except (OSError, subprocess.SubprocessError, ValueError):
                    resources['notes'].append('A process sample failed'); previous_sample = None
            if metrics['complete']:
                if recovery.get('recovered_at') and 'rebuild_complete_ms' not in recovery:
                    recovery['rebuild_complete_ms'] = (time.monotonic() - recovery_start) * 1000
                break
            time.sleep(.01)
        else:
            raise RuntimeError('Finite replay timed out')
        rows = status['discovery_table']; dump(output / f'rows-{number}.json', rows)
        conclusions = summarize(rows)
        # Browser loads the real UI in the backend network namespace.
        analytics = json.loads(browser(['analytics', 'http://127.0.0.1:8080', f'/results/rows-{number}.json'],
                                       output, prefix+'-browser', f'container:{container}'))
        conclusions.update(analytics)
        scenario = scenario_checks(baseline, rows)
        scenario['anomaly_ui_conclusion'] = any(p['key'] == 'ble:02:00:00:00:99:01' and
            'Strong local signal; review proximity' in p['anomalies'] for p in analytics['patterns'])
        counts = accounting(metrics)
        recovery['post_recovery_events_accepted'] = counts['accepted'] - paused_count if paused_count is not None else 0
        checks = {'accounting_reconciles': reconciles(counts), 'all_fixture_events_accepted': counts['accepted'] == len(events),
                  'zero_rejections_or_drops': counts['rejected'] == counts['dropped'] == 0,
                  'all_event_ids_observed': set(observed) == {e['validation_event_id'] for e in events},
                  'valid_latency_samples': all(v >= 0 and math.isfinite(v) for v in observed.values()),
                  'interruption_detected': recovery['interruption_detected'],
                  'recovered': recovery['recovered'] and recovery['post_recovery_events_accepted'] > 0,
                  **scenario}
        if fault == 'restart':
            checks['post_restart_state_clean'] = recovery.get('post_restart_state_clean', False)
        resources['restart_count'] = int(command(['docker', 'inspect', '-f', '{{.RestartCount}}', container], capture_output=True).stdout)
        resources['entity_count_at_completion'] = len(rows)
        resources['ending_rss_bytes'] = resources['samples'][-1]['rss_bytes'] if resources['samples'] else None
        resources['rss_growth_bytes'] = (resources['ending_rss_bytes'] - resources['starting_rss_bytes']
                                        if resources['samples'] else None)
        return dict(run=number, duration_s=time.monotonic()-started, event_accounting=counts,
                    latency_ms=percentiles(list(observed.values())), latency_samples_ms=list(observed.values()),
                    latency_trend=latency_trend(list(observed.values())),
                    recovery=recovery, resources=resources, conclusions=conclusions,
                    scenario_checks=scenario, checks=checks, overall_status='pass' if all(checks.values()) else 'fail')
    except (Exception, KeyboardInterrupt) as exc:
        dump(output / f'run-{number}.json', {
            'run': number, 'overall_status': 'fail', 'error': str(exc).replace(str(ROOT), '<repository>'),
            'event_accounting': accounting(status['validation_metrics']) if 'status' in locals() else None,
            'recovery': recovery, 'resources': resources,
            'latency_ms': percentiles(list(observed.values())),
        })
        raise
    finally:
        with log_path.open('w') as log:
            subprocess.run(['docker', 'logs', container], stdout=log, stderr=subprocess.STDOUT, text=True)
        resources['log_bytes'] = log_path.stat().st_size
        cleanup = subprocess.run(['docker', 'rm', '-f', container], capture_output=True, text=True)
        if cleanup.returncode and 'No such container' not in cleanup.stderr:
            raise RuntimeError(f'Container cleanup failed for {container}: {cleanup.stderr.strip()}')


BUNDLE_README = """RF Sentinel validation evidence bundle (synthetic replay).

MANIFEST.json lists SHA-256 of every file. Verify independently with:
    python3 scripts/verify_evidence_bundle.py <this archive>

To replay the exact fixture through the demo backend:
    docker build -f Dockerfile.demo -t rf-sentinel:public-demo .
    docker run --rm -p 127.0.0.1:8080:8080 -e RF_SENTINEL_DEMO_LOOP=0 \\
        -e RF_SENTINEL_DEMO_REFERENCE_TIMESTAMP=1795348800 \\
        -v "$PWD/events.jsonl:/app/.demo/events/public-demo-events.jsonl:ro" rf-sentinel:public-demo

Contents: results.json (aggregate), run-N.json and run-restart.json (per run), rows-*.json (API discovery
rows used for Pattern-of-Life analytics), events.jsonl (fixture), result-card.html/png, logs/ (paths redacted).
"""


def redact(text):
    for path, label in ((str(ROOT), '<repository>'), (str(Path.home()), '<home>')):
        text = text.replace(path, label)
    return text


def write_bundle(output, result):
    """Package evidence with per-file checksums; logs are redacted of host paths."""
    import io
    import tarfile
    names = ['events.jsonl', 'results.json', 'result-card.html', 'result-card.png']
    names += sorted(p.name for p in output.glob('run-*.json')) + sorted(p.name for p in output.glob('rows-*.json'))
    names += sorted('logs/' + p.name for p in (output / 'logs').glob('*.log'))
    contents = {}
    for name in names:
        path = output / name
        if path.exists():
            data = path.read_bytes()
            contents[name] = redact(data.decode(errors='replace')).encode() if name.startswith('logs/') else data
    contents['README.txt'] = BUNDLE_README.encode()
    manifest = dict(schema_version='1.0', application_version=result['application_version'],
                    source_sha256=result.get('source_sha256'), overall_status=result['overall_status'],
                    configuration=result['configuration'], created_at=utc_now(),
                    files={name: hashlib.sha256(data).hexdigest() for name, data in sorted(contents.items())})
    contents['MANIFEST.json'] = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    folder = output / 'bundle'; folder.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    commit, _, dirty = result['application_version'].partition('-')
    archive = folder / f"rf-sentinel-validation-{commit[:12]}{'-' + dirty if dirty else ''}-{stamp}.tar.gz"
    with tarfile.open(archive, 'w:gz') as tar:
        for name, data in sorted(contents.items()):
            info = tarfile.TarInfo('rf-sentinel-validation/' + name); info.size = len(data); info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    (folder / (archive.name + '.sha256')).write_text(f'{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n')
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-build', action='store_true', help='Use locally built images (development only)')
    parser.add_argument('--output', type=Path, default=ROOT / '.demo/validation',
                        help='Evidence directory; use a separate directory for a soak')
    args = parser.parse_args()
    config = {'duration_s': float(os.getenv('RF_SENTINEL_VALIDATION_DURATION_S', '30')),
              'rate': float(os.getenv('RF_SENTINEL_VALIDATION_RATE', '10')),
              'runs': int(os.getenv('RF_SENTINEL_VALIDATION_RUNS', '3')),
              'reference_timestamp': 1795348800, 'poll_interval_s': .01}
    if not all(math.isfinite(config[k]) and config[k] > 0 for k in ('duration_s', 'rate')) or config['runs'] < 3:
        parser.error('duration and rate must be finite and positive; at least three runs are required')
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    (output / 'logs').mkdir(exist_ok=True)
    # An exclusive lock prevents concurrent runs overwriting the evidence package.
    import fcntl
    lock = (output / '.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    for old in (output / 'logs').glob('*.log'):
        old.unlink()
    for pattern in ('run-*.json', 'rows-*.json', 'result-card.*', 'results.json', 'bundle/*'):
        for old in output.glob(pattern): old.unlink()
    events = build_validation_events(config['duration_s'], config['rate'])
    fixture_path = output / 'events.jsonl'
    fixture_path.write_text(''.join(json.dumps(e, sort_keys=True) + '\n' for e in events))
    fixture = dict(path=str(fixture_path.relative_to(ROOT)) if fixture_path.is_relative_to(ROOT) else fixture_path.name, sha256=hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
                   event_count=len(events), protocols=sorted({e['protocol'] for e in events}))
    source_paths = [ROOT / '.dockerignore', ROOT / 'Dockerfile.demo', ROOT / 'Dockerfile.validation', ROOT / 'requirements.txt',
                    ROOT / 'pyproject.toml', ROOT / 'rf_platform/ui.py', ROOT / 'ui/backend/app.py', ROOT / 'ui/frontend/index.html']
    source_paths += sorted((ROOT / 'scripts').glob('*.py')) + sorted((ROOT / 'scripts').glob('*.sh')) + sorted((ROOT / 'tests').glob('*.py'))
    source_digest = digest({str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths})
    version = command(['git', 'rev-parse', 'HEAD'], capture_output=True).stdout.strip()
    # Dirty = any tracked change, or untracked files that can reach the images or harness (not untracked docs).
    if command(['git', 'status', '--porcelain', '--', '.', ':(exclude)docs/*.md'], capture_output=True).stdout.strip():
        version += '-dirty'
    result = new_result(version, fixture, config); runs = []; started = time.monotonic()
    result['source_sha256'] = source_digest
    prefix = 'rf-validation-' + uuid.uuid4().hex[:10]
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'Signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if not args.skip_build:
            for dockerfile, tag in [('Dockerfile.demo', 'rf-sentinel:public-demo'), ('Dockerfile.validation', 'rf-sentinel:validation')]:
                print(f'Building {tag}', flush=True)
                with (output / 'logs' / (dockerfile+'.log')).open('w') as log:
                    command(['docker', 'build', '-f', dockerfile, '-t', tag, '.'], stdout=log, stderr=subprocess.STDOUT)
        print('Running regression and harness tests', flush=True)
        with (output / 'logs/tests.log').open('w') as log:
            command(['docker', 'run', '--rm', '--name', prefix+'-tests', 'rf-sentinel:validation'], stdout=log, stderr=subprocess.STDOUT)
        for number in range(1, config['runs']+1):
            print(f'Validation run {number}/{config["runs"]}', flush=True)
            run = run_once(number, fixture_path, events, config, output, prefix)
            dump(output / f'run-{number}.json', run); runs.append(run)
        print('Restart-recovery run', flush=True)
        restart = run_once('restart', fixture_path, events, config, output, prefix, fault='restart')
        dump(output / 'run-restart.json', restart)
        result['repeatability'] = compare_runs(runs)
        result['conclusions'] = runs[0]['conclusions']
        result['event_accounting'] = {key: sum(r['event_accounting'][key] for r in runs) for key in ('attempted','accepted','rejected','dropped')}
        reasons = Counter()
        for run in runs: reasons.update(run['event_accounting']['rejection_reasons'])
        result['event_accounting']['rejection_reasons'] = dict(reasons)
        result['latency_ms'] = percentiles([v for r in runs for v in r['latency_samples_ms']])
        result['recovery'] = {**runs[0]['recovery'],
            'recovery_time_ms': max(r['recovery']['recovery_time_ms'] or 0 for r in runs),
            'post_recovery_events_accepted': sum(r['recovery']['post_recovery_events_accepted'] for r in runs)}
        for key in ('interruption_injected', 'interruption_detected', 'recovered'):
            result['recovery'][key] = all(r['recovery'][key] for r in runs)
        result['latency_trends'] = [r['latency_trend'] for r in runs]
        result['resource_trends'] = [{k: r['resources'][k] for k in ('starting_rss_bytes', 'ending_rss_bytes', 'rss_growth_bytes')} for r in runs]
        result['resources'] = dict(runs[0]['resources'])
        result['resources'].pop('samples', None)
        result['resources']['notes'] = sorted({note for run in runs for note in run['resources']['notes']})
        result['resources']['restart_count'] = sum(run['resources']['restart_count'] for run in runs)
        result['recovery']['aggregation'] = 'Booleans require all runs; time is maximum; accepted count is sum; timestamps describe run 1.'
        for key in ('peak_rss_bytes','peak_cpu_percent'):
            values = [r['resources'][key] for r in runs if r['resources'][key] is not None]
            result['resources'][key] = max(values) if values else None
        result['checks'] = [{'name': f'run-{r["run"]}', 'passed': r['overall_status']=='pass'} for r in runs]
        result['checks'].append({'name':'repeatability', 'passed':result['repeatability']['all_stable_results_match']})
        result['soak'] = soak_summary(runs)
        rec = restart['recovery']
        result['restart_recovery'] = {key: rec.get(key) for key in (
            'fault', 'interruption_detected', 'recovered', 'exit_code', 'api_unreachable_after_kill',
            'pre_kill_events_accepted', 'pre_kill_entity_count', 'state_after_restart', 'post_restart_state_clean',
            'kill_to_health_ms', 'recovery_time_ms', 'rebuild_complete_ms')}
        result['restart_recovery'].update(
            event_accounting=restart['event_accounting'], latency_ms=restart['latency_ms'],
            result_digest=digest(stable_result(restart)),
            rebuilt_matches_uninterrupted=digest(stable_result(restart)) == result['repeatability']['normalized_result_digest'],
            note='Detection state is in memory. After SIGKILL it is lost, then rebuilt by replaying the source fixture; '
                 'this is not durable-state recovery.')
        result['checks'].append({'name': 'run-restart', 'passed': restart['overall_status'] == 'pass'})
        result['checks'].append({'name': 'restart_rebuild_matches', 'passed': result['restart_recovery']['rebuilt_matches_uninterrupted']})
        result['overall_status'] = 'pass' if all(c['passed'] for c in result['checks']) else 'fail'
    except (Exception, KeyboardInterrupt) as exc:
        result['checks'].append({'name':'execution', 'passed':False, 'error':str(exc).replace(str(ROOT), '<repository>')})
        print(f'Validation failed: {exc}', file=sys.stderr)
    finally:
        for suffix in ('browser', 'tests', 'restart'):
            subprocess.run(['docker','rm','-f', prefix+'-'+suffix], capture_output=True)
        result['completed_at'] = utc_now(); result['duration_s'] = time.monotonic()-started
        result['resources']['log_bytes'] = sum(p.stat().st_size for p in (output/'logs').glob('*'))
        dump(output/'results.json', result)
        (output/'result-card.html').write_text(result_card(json.loads((output/'results.json').read_text())))
        try:
            browser(['card','/results/result-card.html','/results/result-card.png'], output, prefix+'-browser')
        except Exception as exc:
            result['overall_status'] = 'fail'
            result['checks'].append({'name':'card_render', 'passed':False, 'error':str(exc).replace(str(ROOT), '<repository>')})
            dump(output/'results.json', result)
            (output/'result-card.html').write_text(result_card(result))
        subprocess.run(['docker','rm','-f', prefix+'-browser'], capture_output=True)
        try:
            bundle = write_bundle(output, result)
            print(f'Evidence bundle: {bundle}', flush=True)
        except Exception as exc:
            print(f'Evidence bundle failed: {exc}', file=sys.stderr)
            result['overall_status'] = 'fail'
    print(f'{result["overall_status"].upper()}: {output / "results.json"}', flush=True)
    return 0 if result['overall_status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
