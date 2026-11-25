from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.generate_demo_events import build_validation_events
from scripts.validate_demo import (accounting, compare_runs, digest, empty_resources, new_result,
                                  percentiles, reconciles, result_card, soak_summary, stable_result,
                                  wait_for_status, write_bundle)
from scripts import verify_evidence_bundle


class ValidationTest(unittest.TestCase):
    def test_schema_and_unavailable_metrics(self):
        result = new_result('commit', {'event_count': 0, 'protocols': []}, {})
        required = {'schema_version','application','application_version','started_at','completed_at',
                    'duration_s','public_safe','synthetic_replay','fixture','event_accounting','latency_ms',
                    'recovery','repeatability','resources','conclusions','checks','overall_status'}
        self.assertTrue(required <= result.keys())
        self.assertTrue({'protocol_counts', 'entity_count', 'wifi_ap_count', 'wifi_station_count',
                         'recurring_pattern_count', 'weekend_pattern_count', 'anomaly_count'}
                        <= result['conclusions'].keys())
        self.assertIsNone(empty_resources()['peak_cpu_percent'])
        self.assertTrue(empty_resources()['notes'])
        json.dumps(result, allow_nan=False)

    def test_fixture_identity_and_scenario(self):
        one = build_validation_events(3, 10)
        self.assertEqual(one, build_validation_events(3, 10))
        self.assertEqual(len(one), len({e['validation_event_id'] for e in one}))
        self.assertEqual([e['validation_sequence'] for e in one], list(range(1, len(one)+1)))
        self.assertEqual({e['scenario_phase'] for e in one}, {'baseline','new_entity','relationship_change','off_cadence_activity','operator_review'})
        self.assertTrue(all(e['scenario_truth_id'] and e['scenario_expected_outcome'] for e in one))

    def test_walkthrough_fixture_paces_scenario_in_order(self):
        from scripts.record_walkthrough import walkthrough_events
        events = walkthrough_events()
        offsets = [e['offset_s'] for e in events]
        self.assertEqual(offsets, sorted(offsets))
        self.assertEqual([e['scenario_phase'] for e in events[-4:]],
                         ['new_entity', 'relationship_change', 'off_cadence_activity', 'operator_review'])
        baseline_end = max(e['offset_s'] for e in events if e['scenario_phase'] == 'baseline')
        self.assertLess(baseline_end, events[-4]['offset_s'])

    def test_nearest_rank(self):
        p = percentiles(list(range(1, 101)))
        self.assertEqual((p['p50'],p['p95'],p['p99']), (50,95,99))
        self.assertEqual(percentiles([7])['p99'],7)
        self.assertIsNone(percentiles([])['p50'])

    def test_reconciliation(self):
        counts = dict(attempted=5,accepted=3,rejected=1,dropped=1,rejection_reasons={'malformed_event':1})
        self.assertTrue(reconciles(counts))
        self.assertFalse(reconciles({**counts, 'accepted':4}))
        self.assertFalse(reconciles({**counts, 'rejection_reasons':{}}))

    def test_normalization_and_mismatch(self):
        run = dict(conclusions={'protocol_counts':{'WIFI':1}, 'entity_count':1, 'patterns':[]},
                   event_accounting={}, scenario_checks={}, duration_s=1)
        other = copy.deepcopy(run); other['duration_s'] = 100
        self.assertTrue(compare_runs([run,other])['all_stable_results_match'])
        other['conclusions']['entity_count'] = 2
        self.assertFalse(compare_runs([run,other])['all_stable_results_match'])
        self.assertFalse(compare_runs([run,other])['entity_counts_match'])
        self.assertEqual(digest({'a':1,'b':2}),digest({'b':2,'a':1}))

    def test_card_is_derived_from_json_and_escaped(self):
        result = new_result('<commit>', {'event_count':123,'protocols':['wifi']}, {})
        result['latency_ms'] = percentiles([17.5])
        card = result_card(json.loads(json.dumps(result)))
        self.assertIn('123 / 1',card)
        self.assertIn('17.5',card)
        self.assertIn('&lt;commit&gt;',card)
        self.assertIn('SYNTHETIC',card)

    def test_health_timeout(self):
        with patch('scripts.validate_demo.api', side_effect=OSError('offline')):
            with self.assertRaisesRegex(RuntimeError, 'timeout'):
                wait_for_status('http://unused',timeout=.01)

    def test_soak_summary_reports_unthresholded_drift(self):
        runs = [{'latency_trend': {'p95_delta_ms': d}, 'resources': {'rss_growth_bytes': g}}
                for d, g in ((1.5, 100), (-2.0, 4096), (None, None))]
        soak = soak_summary(runs)
        self.assertEqual((soak['max_p95_delta_ms'], soak['max_rss_growth_bytes']), (1.5, 4096))
        self.assertIsNone(soak_summary([])['max_p95_delta_ms'])

    def test_card_shows_restart_and_soak_from_json(self):
        result = new_result('c', {'event_count': 1, 'protocols': []}, {})
        result['restart_recovery'] = {'rebuilt_matches_uninterrupted': True, 'kill_to_health_ms': 812.25}
        result['soak'] = {'max_p95_delta_ms': 3.5, 'max_rss_growth_bytes': 2 * 2**20}
        card = result_card(result)
        self.assertIn('812.25', card); self.assertIn('3.50', card); self.assertIn('2.00', card)
        self.assertIn('unavailable', result_card(new_result('c', {'event_count': 1, 'protocols': []}, {})))

    def _evidence(self, folder):
        run = dict(conclusions={'entity_count': 1, 'protocol_counts': {}, 'patterns': []}, scenario_checks={'ok': True},
                   event_accounting=dict(attempted=2, accepted=2, rejected=0, dropped=0, rejection_reasons={}))
        (folder / 'logs').mkdir()
        (folder / 'logs' / 'run-1.log').write_text(f'started in {Path(__file__).resolve().parents[1]}\n')
        (folder / 'events.jsonl').write_text('{"a":1}\n{"a":2}\n')
        import hashlib
        result = new_result('abc', {'event_count': 2, 'protocols': [],
                                    'sha256': hashlib.sha256((folder / 'events.jsonl').read_bytes()).hexdigest()}, {})
        runs = [run, copy.deepcopy(run), copy.deepcopy(run)]
        result['repeatability'] = compare_runs(runs); result['overall_status'] = 'pass'
        result['restart_recovery'] = {'result_digest': digest(stable_result(run))}
        for n, r in enumerate(runs, 1):
            (folder / f'run-{n}.json').write_text(json.dumps(r))
        (folder / 'run-restart.json').write_text(json.dumps(run))
        (folder / 'results.json').write_text(json.dumps(result))
        return result

    def test_bundle_round_trip_and_tamper_detection(self):
        import tarfile
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'evidence'; folder.mkdir()
            archive = write_bundle(folder, self._evidence(folder))
            self.assertTrue((archive.parent / (archive.name + '.sha256')).exists())
            with tarfile.open(archive) as tar:
                tar.extractall(Path(temp) / 'x')
            root = Path(temp) / 'x' / 'rf-sentinel-validation'
            self.assertNotIn(str(Path(__file__).resolve().parents[1]), (root / 'logs/run-1.log').read_text())
            self.assertTrue(all(ok for _, ok, _ in verify_evidence_bundle.verify(root)))
            (root / 'run-2.json').write_text(json.dumps({'tampered': True}))
            with self.assertRaises(KeyError):
                verify_evidence_bundle.verify(root)
            (root / 'run-2.json').write_text((root / 'run-1.json').read_text() + ' ')
            failed = [name for name, ok, _ in verify_evidence_bundle.verify(root) if not ok]
            self.assertEqual(failed, ['checksum run-2.json'])


class BackendValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        env = {'RF_SENTINEL_DEMO_MODE':'1', 'RF_SENTINEL_DEMO_START_PAUSED':'1',
               'RF_SENTINEL_LOG_DIR':cls.temp.name, 'RF_SENTINEL_TEXTUAL_CONSOLE':'0',
               'RF_SENTINEL_CONSOLE_DASHBOARD':'0', 'RF_SENTINEL_DEMO_REFERENCE_TIMESTAMP':'1795348800'}
        cls.env = patch.dict(os.environ,env); cls.env.start()
        from rf_platform.ui import _load_backend
        # Do not start background workers in unit tests. Replay scheduling has its own test.
        with patch('threading.Thread.start'):
            cls.backend = _load_backend()
        cls.backend.BLE_IDENTITY_CACHE_PATH = Path(cls.temp.name) / 'ble_identities.json'
        cls.backend.ble_identity_cache.clear()
        cls.client = cls.backend.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop(); cls.temp.cleanup()

    def setUp(self):
        b = self.backend
        b.shutdown_complete = False
        b.demo_replay_paused.set()
        with b.state_lock:
            b._reset_stats()
            b._demo_prime_state()

    def test_status_preserves_fields_and_counters(self):
        status = self.client.get('/api/status').get_json()
        self.assertTrue({'running','mode','discovery_table','detections','available_devices','validation_metrics'} <= status.keys())
        self.assertTrue(self.backend._demo_ingest_payload(build_validation_events()[0]))
        status = self.client.get('/api/status').get_json()
        metrics = status['validation_metrics']
        self.assertEqual(metrics['events_accepted'],1)
        self.assertEqual(metrics['events_attempted'],1)
        self.assertEqual(status['detections'][0]['validation_event_id'],metrics['receipts'][0]['event_id'])
        self.assertGreaterEqual(metrics['receipts'][0]['visible_at'],metrics['receipts'][0]['emitted_at'])

    def test_receipts_cursor_returns_only_new_receipts(self):
        events = build_validation_events()
        for event in events[:3]:
            self.assertTrue(self.backend._demo_ingest_payload(event))
        full = self.client.get('/api/status').get_json()['validation_metrics']['receipts']
        self.assertEqual([r['accepted_index'] for r in full], [1, 2, 3])
        tail = self.client.get('/api/status?receipts_after=2').get_json()['validation_metrics']
        self.assertEqual([r['event_id'] for r in tail['receipts']], [events[2]['validation_event_id']])
        self.assertEqual(tail['events_accepted'], 3)
        self.assertEqual(self.client.get('/api/status?receipts_after=3').get_json()['validation_metrics']['receipts'], [])

    def test_rejection_does_not_increment_acceptance(self):
        for invalid in ({}, {'protocol':'ble'}, None):
            self.assertFalse(self.backend._demo_ingest_payload(invalid))
        metrics = self.client.get('/api/status').get_json()['validation_metrics']
        self.assertEqual(metrics['events_accepted'],0)
        self.assertEqual(metrics['rejection_reasons'],{'malformed_event':3})
        self.assertTrue(reconciles(accounting(metrics)))

    def test_invalid_json_records_are_accounted(self):
        b = self.backend
        fixture = Path(self.temp.name) / 'malformed.jsonl'
        fixture.write_text('not json\n[]\n{"offset_s":"bad"}\n')
        with patch.object(b, 'RF_SENTINEL_DEMO_EVENT_FILE', fixture):
            records = b._load_demo_payloads()
        self.assertEqual(len(records), 3)
        for record in records:
            self.assertFalse(b._demo_ingest_payload(record))
        self.assertEqual(b.validation_metrics['rejection_reasons'], {'malformed_event': 3})

    def test_filtered_and_invisible_events_rejected(self):
        b = self.backend
        self.assertFalse(b._demo_ingest_payload({'protocol':'unknown','kind':'unknown'}))
        self.assertFalse(b._demo_ingest_payload({'protocol':'ble','kind':'ble_burst'}))
        self.assertEqual(b.validation_metrics['events_accepted'], 0)
        self.assertEqual(b.validation_metrics['rejection_reasons'],
                         {'normalizer_filtered':1, 'not_api_visible':1})

    def test_pause_resume_and_clear(self):
        b = self.backend
        b._demo_ingest_payload(build_validation_events()[0])
        self.client.post('/api/scan/stop',json={})
        self.assertFalse(self.client.get('/api/status').get_json()['running'])
        self.client.post('/api/scan/start',json={'mode':'sentinel','preserve_detections':True})
        status = self.client.get('/api/status').get_json()
        self.assertTrue(status['running'])
        self.assertEqual(status['validation_metrics']['events_accepted'],1)
        self.client.post('/api/clear',json={})
        status = self.client.get('/api/status').get_json()
        self.assertEqual(status['validation_metrics']['events_attempted'],0)
        self.assertEqual(status['discovery_table'],[])

    def test_finite_replay_paused_during_wait_does_not_skip(self):
        import threading
        import time
        b = self.backend
        events = build_validation_events(1,100)[:3]
        for idx,event in enumerate(events): event['offset_s'] = .1 * (idx+1)
        with patch.object(b,'_load_demo_payloads',return_value=events), patch.object(b,'RF_SENTINEL_DEMO_LOOP',False):
            b.demo_replay_paused.clear()
            worker = threading.Thread(target=b._demo_replay_loop)
            worker.start()
            try:
                time.sleep(.04)
                self.client.post('/api/scan/stop',json={})
                time.sleep(.15)
                self.assertEqual(b.validation_metrics['events_attempted'],0)
                self.client.post('/api/scan/start',json={'mode':'sentinel','preserve_detections':True})
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(b.validation_metrics['events_accepted'],3)
                self.assertTrue(b.validation_metrics['complete'])
            finally:
                b.shutdown_complete = True
                worker.join(3)

    def test_analyst_endpoints_are_read_only_and_grounded(self):
        b = self.backend
        b.RF_SENTINEL_ANALYST_DB = Path(self.temp.name) / 'analyst-test.sqlite'
        self.assertEqual(self.client.post('/api/analyst/investigate', json={}).status_code, 409)
        self.assertEqual(self.client.get('/api/analyst/report').status_code, 404)
        events = build_validation_events()
        baseline_count = sum(e['scenario_phase'] == 'baseline' for e in events)
        for event in events[:baseline_count]:
            b._demo_ingest_payload(event)
        baseline = self.client.post('/api/analyst/baseline', json={}).get_json()
        self.assertTrue(baseline['snapshot_id'].startswith('snapshot:baseline:'))
        for event in events[baseline_count:]:
            b._demo_ingest_payload(event)
        before = self.client.get('/api/status').get_json()['validation_metrics']['events_accepted']
        inv = self.client.post('/api/analyst/investigate', json={}).get_json()
        self.assertTrue(inv['grounding']['passed'], inv['grounding']['issues'])
        self.assertEqual(inv['executed_actions'], [])
        self.assertIn('new_entity', [f['category'] for f in inv['findings']])
        self.assertNotIn('scenario_', json.dumps(inv))
        after = self.client.get('/api/status').get_json()['validation_metrics']['events_accepted']
        self.assertEqual(before, after)  # investigation does not touch ingestion state
        latest = self.client.get('/api/analyst/latest').get_json()
        self.assertEqual(latest['investigation']['investigation_id'], inv['investigation_id'])
        page = self.client.get('/api/analyst/report')
        self.assertEqual(page.status_code, 200)
        self.assertIn(inv['investigation_id'], page.get_data(as_text=True))
        # Persistent memory: the assessment is on record, annotations are separate, nothing new to compare.
        history = self.client.get('/api/analyst/history').get_json()
        self.assertEqual([a['investigation_id'] for a in history['assessments']], [inv['investigation_id']])
        self.assertEqual(history['baselines'][0]['baseline_id'], 'baseline:default:v1')
        note = self.client.post('/api/analyst/annotate', json={'kind': 'known_device', 'text': 'Lab beacon',
                                                               'entity_key': 'ble:02:00:00:00:99:01'})
        self.assertEqual(note.status_code, 200)
        self.assertEqual(self.client.post('/api/analyst/annotate', json={'kind': 'bogus', 'text': 'x',
                                                                         'entity_key': 'k'}).status_code, 400)
        again = self.client.post('/api/analyst/investigate', json={'against': 'last'})
        self.assertEqual(again.status_code, 409)  # nothing captured since the last assessment
        self.assertIn('Nothing new', again.get_json()['error'])

    def test_live_status_does_not_expose_truth(self):
        b = self.backend
        with patch.object(b,'RF_SENTINEL_DEMO_MODE',False), patch.object(b,'_sync_scanner_assignment_centers_from_gateway',return_value={}), patch.object(b,'_available_devices',return_value=[]):
            self.assertNotIn('validation_metrics', self.client.get('/api/status').get_json())


if __name__ == '__main__':
    unittest.main()
