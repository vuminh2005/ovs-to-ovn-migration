"""Ordering boundaries and the supplied TXT presentation contract."""
import hashlib
import unittest
import yaml
from test_validation import ROOT


def plays(name):
    return yaml.safe_load((ROOT/name).read_text())


def tasks(name):
    return [task for play in plays(name) for task in play.get('tasks',[])]


class WorkflowTests(unittest.TestCase):
    def test_preparation_and_existing_capture_check_precede_freeze(self):
        rows=tasks('playbooks/07-migrate-db.yml')
        names=[row['name'] for row in rows]
        freeze=names.index('[07] Stop and disable neutron-server')
        for name in ('[07] Resolve neutron-server image',
                     '[07] [SAVE] Stage router backend association helper',
                     '[07] [SAVE] Copy existing VM port IDs to OVN CLI host',
                     '[07] Enable OpenFlow15 on br-int',
                     '[07] Check original Group A capture and measurement anchor'):
            self.assertLess(names.index(name),freeze)
        self.assertGreater(names.index('[07] Run neutron-ovn-db-sync-util migrate'),freeze)
        phase08=str(plays('playbooks/08-cutover.yml'))
        self.assertIn('openflow_check_only',phase08)
        self.assertNotIn('ovs-vsctl set Bridge',phase08)
        self.assertIn('sha256sum --check --status',phase08)
        self.assertNotIn('anchor-start',phase08)

    def test_continuous_observers_and_special_conntrack_remain_but_checkpoint_dispatches_leave_cutover(self):
        for phase in ('08-cutover','09-cleanup','10-restore-neutron'):
            text=(ROOT/'playbooks'/f'{phase}.yml').read_text()
            self.assertNotIn('diagnostics-checkpoint-tasks.yml',text)
        text=(ROOT/'playbooks/08-cutover.yml').read_text()
        self.assertIn('neutron-ovn-metadata-agent',text)
        self.assertIn('scripts/ns_diagnostics.py',text)
        self.assertIn('- post-cutover',text)

    def test_recovery_observation_precedes_after_snapshot_and_smoke_test(self):
        rows=tasks('playbooks/11-validate.yml'); names=[row['name'] for row in rows]
        gate=next(i for i,row in enumerate(rows) if 'live_recovery.py' in row.get('ansible.builtin.shell',''))
        after=names.index('[11] [SAVE] Record resources before creating smoke tests')
        self.assertLess(gate,after)
        gate_task=rows[gate]
        self.assertIn('phase11.end',str(gate_task['when']))
        self.assertNotIn('ignore_errors',gate_task)
        self.assertTrue(plays('playbooks/11-validate.yml')[0]['any_errors_fatal'])
        self.assertIn('post_restore_observation_timeout_seconds',gate_task['ansible.builtin.shell'])

    def test_reset_cleans_preparation_after_destroy_and_prepares_openflow_after_deploy(self):
        rows=tasks('reset-lab-to-ovs.yml'); names=[row['name'] for row in rows]
        cleanup=names.index('Validate ownership before removing any run preparation directory')
        destroy=names.index('Run kolla-ansible destroy')
        self.assertGreater(cleanup,destroy)
        self.assertIn('scripts/cutover_preparation.py cleanup /tmp',rows[cleanup]['ansible.builtin.script'])
        of=names.index('Prepare additive OpenFlow15 baseline before creating any workload')
        self.assertGreater(of,cleanup)
        self.assertLess(of,names.index('Verify neutron_openvswitch_agent is running'))

    def test_txt_renderer_and_helpers_are_byte_identical_to_supplied_final_zip(self):
        self.assertEqual(hashlib.sha256((ROOT/'scripts/report_format.py').read_text().partition('\ndef terminal_summary(')[0].encode()).hexdigest(),
            '3ca3e49f1507dfdf18bcfbfed48139051d98b194580be06be16344e588a62157')


if __name__=='__main__': unittest.main()
