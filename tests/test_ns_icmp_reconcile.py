"""Captured local-unreachable gap: classify attempts without inventing wire loss."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from test_validation import ROOT, ready_evidence, v
from ns_icmp_reconcile import reconcile
from diagnostic_policy import coverage
from report_format import render_text


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=json.loads((ROOT/'tests/fixtures/ns-fip-local-errors.json').read_text())

    def result(self, data=None, sender=None):
        f=data or self.fixture
        return reconcile(f['value'],f['rows'],f['start'],f['end'],
            sender or dict(status='PASS'),f['observations'],f['interval'],f['source_ip'])

    def test_actual_outage_has_exact_local_errors_and_keeps_raw_packet_metric_unavailable(self):
        before=copy.deepcopy(self.fixture)
        value=self.result()
        self.assertEqual(value['status'],'PASS')
        self.assertEqual(value['local_send_failures'],30)
        self.assertEqual(value['probe_failures'],self.fixture['value']['packets_failed']+30)
        self.assertAlmostEqual(value['actual_dataplane_outage_seconds'],94.70844602584839)
        self.assertEqual(value['failure_burst_count'],1)
        self.assertIsNone(value['packet_loss_percent'])
        self.assertEqual(value['wire_capture_metrics']['status'],'UNAVAILABLE')
        self.assertEqual(self.fixture,before)

    def test_missing_misidentified_duplicated_or_remote_errors_stay_unavailable(self):
        for mode in ('no-error','wrong-source','wrong-sequence','duplicate-error','reply-conflict','clock','identity','capture-drop','new-burst'):
            f=copy.deepcopy(self.fixture)
            error=next(o for o in f['observations'] if o['kind']=='error')
            if mode=='no-error':error['kind']='outstanding'
            elif mode=='wrong-source':error['text']=error['text'].replace('10.1.0.187','10.1.0.1')
            elif mode=='wrong-sequence':error['icmp_sequence']+=100
            elif mode=='duplicate-error':f['observations'].append(dict(error))
            elif mode=='reply-conflict':f['observations'].append(dict(error,kind='reply'))
            elif mode=='clock':error['native_ts']+=10
            elif mode=='identity':f['rows'][3]['identifier']+=1
            elif mode=='capture-drop':f['value']['reason']+='; Continuous capture lifetime/drop-free completion not proven'
            elif mode=='new-burst':
                prior=next(r for r in f['rows'] if r['icmp_sequence']==8250)
                prior['reply_timestamp']=prior['ts']+.001
            with self.subTest(mode=mode):
                self.assertEqual(self.result(f)['status'],'UNAVAILABLE')

    def test_incomplete_sender_log_never_authorizes_reconciliation(self):
        self.assertEqual(self.result(sender=dict(status='UNAVAILABLE'))['status'],'UNAVAILABLE')

    def test_batched_requests_with_equal_capture_microsecond_are_not_fake_missing_probes(self):
        f=copy.deepcopy(self.fixture)
        prior=next(r for r in f['rows'] if r['icmp_sequence']==8282)
        current=next(r for r in f['rows'] if r['icmp_sequence']==8283)
        current['ts']=prior['ts']
        self.assertEqual(self.result(f)['status'],'PASS')
        current['ts']=prior['ts']-.001
        self.assertEqual(self.result(f)['status'],'UNAVAILABLE')

    def test_report_labels_probe_attempts_and_preserves_raw_packet_metric(self):
        value=self.result()
        report=dict(north_south=dict(icmp_enabled=True,directions=dict(fip=dict(icmp=value))))
        text=render_text(report,Path('/run'),modern=True).split('Inbound (FIP, ICMP)')[1]
        self.assertIn('Probe failure rate',text)
        self.assertIn('Local send failures',text)
        self.assertIn('94.708 s',text)
        self.assertNotIn('Packet loss',text)
        self.assertNotIn('Port_Binding',text)
        self.assertEqual(value['wire_capture_metrics']['status'],'UNAVAILABLE')


class DiagnosticPolicyTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);ready_evidence(self.root)
        (self.root/'validation-config.json').write_text('{}')
        (self.root/'diagnostics-config.json').write_text('{"enabled":true}')

    def write_diagnostics(self,**changes):
        data=dict(status='UNAVAILABLE',collection_status='STOPPED',collectors=[dict(
            host='compute1',role='core',status='UNAVAILABLE',errors=['openflow-watch'],
            jobs={'openflow-watch':dict(gaps=[dict(duration_seconds=1.091)])})])
        data.update(changes)
        (self.root/'migration-diagnostics.json').write_text(json.dumps(data))
        return data

    def test_reconnected_observer_gap_warns_without_invalidating_certified_traffic(self):
        data=self.write_diagnostics()
        self.assertTrue(v.validation_ready(self.root))
        warning=coverage(data)
        self.assertEqual(warning['status'],'GAPPED')
        text=render_text(dict(diagnostic_coverage=warning),self.root,modern=True)
        self.assertIn('Diagnostics coverage',text)
        self.assertIn('compute1/core',text)
        self.assertIn('1.091 s',text)

    def test_missing_or_unproven_shutdown_still_blocks_cleanup(self):
        self.write_diagnostics(collection_status='UNAVAILABLE')
        self.assertFalse(v.validation_ready(self.root))
        self.write_diagnostics(status='PASS',collection_status='UNAVAILABLE')
        self.assertFalse(v.validation_ready(self.root))

    def test_real_traffic_failure_still_blocks_despite_clean_observer_shutdown(self):
        self.write_diagnostics()
        (self.root/'tenant-dataplane-probe.json').write_text('{"status":"UNAVAILABLE"}')
        self.assertFalse(v.validation_ready(self.root))


if __name__=='__main__':
    unittest.main()
