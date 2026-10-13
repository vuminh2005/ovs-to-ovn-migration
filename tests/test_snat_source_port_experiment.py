"""Opt-in source-port experiment must never weaken held-session validation."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
import configparser
import jinja2
import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from extended_validation import Extended, snat_source_port_status
from ns_probe import Stats


def stats(port=51547, translated=51547):
    row=Stats().snapshot()
    session=dict(server='endpoint',server_boot='boot-srv',listener='listener',connection='held-id')
    row.update(attempts=20,successes=20,consecutive_successes=20,connection_attempts=1,
        first_session=session,last_session=copy.deepcopy(session),observed_peer_ips=['10.1.0.4'],
        first_local_endpoint=['10.233.0.11',port],last_local_endpoint=['10.233.0.11',port],
        first_peer_endpoint=['10.1.0.4',translated],last_peer_endpoint=['10.1.0.4',translated])
    return row


class SourcePortExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.cfg=dict(run='run',timeout=2,ns=dict(enabled=True,preserve_snat_source_port=True,
            snat_ip='10.1.0.4',endpoint_ip='10.1.0.187'))
        self.x=Extended(NS(root=self.root,cfg=self.cfg,cloud=Mock()))
        self.x.state['processes']={}

    def clients(self, translated=51547):
        row=dict(boot='boot-guest',probe_id='probe',mono=100,streams={'held':stats(translated=translated),'new':stats()})
        fip=copy.deepcopy(row)
        for stream in fip['streams'].values():stream['observed_peer_ips']=['10.1.0.187']
        return dict(snat=row,fip=fip)

    def test_comparison_uses_ports_even_when_snat_changes_ip(self):
        self.assertEqual(snat_source_port_status(stats()),'PASS')
        self.assertEqual(snat_source_port_status(stats(translated=45042)),'FAIL')

    def test_missing_invalid_and_late_changed_ports_cannot_pass(self):
        for fault in ('missing','boolean','out-of-range','last-changed'):
            row=stats()
            if fault=='missing':row.pop('first_peer_endpoint')
            if fault=='boolean':row['first_peer_endpoint'][1]=True
            if fault=='out-of-range':row['first_peer_endpoint'][1]=65536
            if fault=='last-changed':row['last_peer_endpoint'][1]=45042
            self.assertIn(snat_source_port_status(row),('FAIL','UNAVAILABLE'))

    def test_mismatched_held_mapping_blocks_before_freeze(self):
        initial=self.clients(translated=45042);fresh=copy.deepcopy(initial)
        for row in fresh.values():row['mono']=101
        with patch.object(self.x,'ns_rows',side_effect=[initial,fresh]), patch.object(self.x,'ns_snapshot',return_value={'tenant_type':'vxlan'}):
            with self.assertRaisesRegex(RuntimeError,'migration blocked before freeze'):
                self.x.ns_wait()
        self.assertFalse((self.root/'ns-baseline.json').exists())

    def test_matching_mapping_creates_a_real_baseline_without_reconnect(self):
        initial=self.clients();fresh=copy.deepcopy(initial)
        for row in fresh.values():row['mono']=101
        with patch.object(self.x,'ns_rows',side_effect=[initial,fresh]), patch.object(self.x,'ns_snapshot',return_value={'tenant_type':'vxlan'}):
            self.x.ns_wait()
        baseline=json.loads((self.root/'ns-baseline.json').read_text())
        self.assertEqual(baseline['clients']['snat']['streams']['held']['connection_attempts'],1)

    def test_broken_held_stays_fail_despite_preserved_port(self):
        baseline={'clients':self.clients()};rows=copy.deepcopy(baseline['clients'])
        for row in rows.values():
            row['mono']=200
            for stream in row['streams'].values():stream['attempts']=40
        rows['snat']['streams']['held'].update(held_broken=True,terminal_error='reset',open_failure_since=120)
        self.x.ns_results(rows,{},baseline,['snat/held reset'])
        result=json.loads((self.root/'ns-results.json').read_text())
        held=result['directions']['snat']['streams']['held']
        self.assertEqual(held['successful_echo_source_port_match'],'PASS')
        self.assertEqual(held['status'],'FAIL')
        self.assertFalse(result['source_snat_profile']['conntrack_transfer'])

    def test_new_run_matching_held_can_pass_only_with_same_session_identity(self):
        baseline={'clients':self.clients()};rows=copy.deepcopy(baseline['clients'])
        for row in rows.values():
            row['mono']=200
            for stream in row['streams'].values():stream['attempts']=40
        self.x.ns_results(rows,{},baseline)
        result=json.loads((self.root/'ns-results.json').read_text())
        self.assertEqual(result['status'],'PASS')
        rows['snat']['streams']['held']['connection_attempts']=2
        self.x.ns_results(rows,{},baseline)
        self.assertEqual(json.loads((self.root/'ns-results.json').read_text())['status'],'FAIL')

    def test_baseline_mapping_mismatch_cannot_be_hidden_by_matching_final_echo(self):
        baseline={'clients':self.clients(translated=45042)};rows=self.clients()
        for row in rows.values():
            row['mono']=200
            for stream in row['streams'].values():stream['attempts']=40
        self.x.ns_results(rows,{},baseline)
        self.assertEqual(json.loads((self.root/'ns-results.json').read_text())['directions']['snat']['held_session_preservation'],'FAIL')

    def test_reset_override_is_opt_in_and_leaves_default_profile_untouched(self):
        plays=yaml.safe_load((ROOT/'reset-lab-to-ovs.yml').read_text())
        override=next(t['ansible.builtin.copy']['content'] for p in plays for t in p.get('tasks',[]) if
            t.get('ansible.builtin.copy',{}).get('dest')=='{{ reset_config_path }}/neutron.conf')
        env=jinja2.Environment();env.filters['bool']=bool
        for enabled in (False,True):
            rendered=env.from_string(override).render(reset_tunnel_mtu=1450,ns_enabled=True,ns_preserve_snat_source_port=enabled)
            parser=configparser.ConfigParser();parser.read_string(rendered)
            self.assertEqual(parser.getint('DEFAULT','global_physnet_mtu'),1450)
            self.assertEqual(parser.has_option('AGENT','use_random_fully'),enabled)
            if enabled:self.assertFalse(parser.getboolean('AGENT','use_random_fully'))

    def test_effective_source_gate_runs_before_workload_creation_and_default_is_off(self):
        variables=yaml.safe_load((ROOT/'group_vars/ns-lab.yml').read_text())
        self.assertFalse(variables['ns_preserve_snat_source_port'])
        precheck=yaml.safe_load((ROOT/'playbooks/02-precheck.yml').read_text())
        gate=next(p for p in precheck if 'SNAT experiment' in p['name'])
        self.assertEqual(gate['hosts'],'network')
        self.assertIn('ns_preserve_snat_source_port',str(gate['tasks'][0]['when']))
        tasks=yaml.safe_load((ROOT/'playbooks/ns-source-profile-tasks.yml').read_text())
        self.assertIn('common.register_iptables_opts',tasks[0]['ansible.builtin.shell'])
        self.assertIn('use_random_fully == false',str(tasks[2]['ansible.builtin.assert']['that']))


if __name__=='__main__':unittest.main()
