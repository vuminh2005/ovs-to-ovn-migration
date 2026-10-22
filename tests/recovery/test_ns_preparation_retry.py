"""Existing EW workloads may survive retry; existing N-S sessions must not restart."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import yaml

from tests.support import ROOT
sys.path.insert(0,str(ROOT/'scripts'))
from ns_preparation_retry import retry_check


class NSPreparationRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'run-one';(self.root/'metrics').mkdir(parents=True)
        self.runtime=dict(phase_marker_schema_version=2,run_id='run-one',inventory='/inventory')
        ns=dict(enabled=True,diagnostics_enabled=True,endpoint_ip='10.1.0.187',snat_ip='10.1.0.4',
            fip_ip='10.1.0.5',outbound_ip='10.233.0.11',port=18180,capture_interface='eth1')
        self.cfg=dict(run='run-one',inventory='/inventory',placement_enabled=True,placement={'0':{},'1':{}},ns=ns,lifetime=14400)
        self.state=dict(schema_version=2,pre={'measure':{str(i):dict(owned=True,port='p'+str(i),server='s'+str(i)) for i in range(2)}})
        self.ext=dict(schema=1,run='run-one',matrix={},ns=dict(router='r',vms={str(i):dict(owned=True,port='n'+str(i)) for i in range(2)}))
        config=dict(run='run-one',kind='endpoint',endpoint_ip=ns['endpoint_ip'],snat_ip=ns['snat_ip'],fip_ip=ns['fip_ip'],
            guest_ip=ns['outbound_ip'],tcp_port=ns['port'],lifetime=14400,allow_create=True,interface='eth1',
            directory=str(self.root/'ns-diagnostics/endpoint-agent'))
        self.diag=dict(schema=1,run='run-one',captures={'endpoint':dict(status='START_INTENT',host='localhost',config=config,launch_attempted=True)})
        self.write()
        for i in range(4):(self.root/'metrics'/f'phase{i:02d}.end').write_text('100')
        (self.root/'metrics/phase04.start').write_text('101')

    def write(self):
        for name,obj in [('runtime.json',self.runtime),('validation-config.json',self.cfg),
                         ('validation-resources.json',self.state),('extended-resources.json',self.ext),
                         ('ns-diagnostics-checkpoint.json',self.diag)]:
            (self.root/name).write_text(json.dumps(obj))

    def test_exact_failed_endpoint_is_eligible_without_mutating_vm_or_capture_journals(self):
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        retry_check(self.root,'/inventory')
        self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_canonical_endpoint_intent_is_eligible(self):
        self.diag['captures']['endpoint']['config']['directory'] = str(self.root/'ns-diagnostics/endpoint')
        self.write()
        retry_check(self.root, '/inventory')

    def test_expanded_capture_retry_checks_ports_and_icmp_in_both_layouts(self):
        self.cfg['ns'].update(new_port=18181, icmp_enabled=True)
        config = self.diag['captures']['endpoint']['config']
        config.update(new_tcp_port=18181, icmp_enabled=True)
        for directory in ('endpoint', 'endpoint-agent'):
            with self.subTest(directory=directory):
                config['directory'] = str(self.root/'ns-diagnostics'/directory)
                self.write()
                retry_check(self.root, '/inventory')
                config['new_tcp_port'] = 18183
                self.write()
                with self.assertRaisesRegex(ValueError, 'differs'):
                    retry_check(self.root, '/inventory')
                config['new_tcp_port'] = 18181

    def test_any_later_marker_blocks_retry(self):
        for name in ('phase04.end','phase05.start','phase06.end','phase07.start','phase08.start',
                     'db_migration.start','control_plane_downtime.start','total.end'):
            with self.subTest(name=name):
                p=self.root/'metrics'/name;p.write_text('')
                with self.assertRaises(ValueError):retry_check(self.root,'/inventory')
                p.unlink()

    def test_any_ns_session_or_guest_blocks_retry(self):
        for fault in ('guest','process','baseline','remote','running','config','inventory'):
            with self.subTest(fault=fault):
                self.write()
                ext=copy.deepcopy(self.ext);diag=copy.deepcopy(self.diag)
                if fault=='guest':ext['ns']['vms']['0']['server']='existing-guest'
                if fault=='process':ext['processes']={'snat-server':{'status':'START_INTENT'}}
                if fault=='remote':diag['captures']['endpoint']['remote']={'status':'RUNNING'}
                if fault=='running':diag['captures']['endpoint']['status']='RUNNING'
                if fault=='config':diag['captures']['endpoint']['config']['snat_ip']='10.1.0.99'
                (self.root/'extended-resources.json').write_text(json.dumps(ext))
                (self.root/'ns-diagnostics-checkpoint.json').write_text(json.dumps(diag))
                baseline=self.root/'ns-baseline.json'
                if fault=='baseline':baseline.write_text('{}')
                with self.assertRaises(ValueError):retry_check(self.root,'/other' if fault=='inventory' else '/inventory')
                baseline.unlink(missing_ok=True)

    def test_retry_preserves_run_and_uses_only_phase04_onward(self):
        plays=yaml.safe_load((ROOT/'archive/playbooks/retry-ns-diagnostics-preparation.yml').read_text())
        imports=[p['import_playbook'] for p in plays if 'import_playbook' in p]
        self.assertEqual(imports,[f'../../playbooks/{name}' for name in (
            '04-validation-workloads.yml','05-stage-ovn-db.yml','06-target-config.yml','07-migrate-db.yml',
            '08-cutover.yml','09-cleanup.yml','10-restore-neutron.yml','11-validate.yml','12-workload-validation.yml','13-report.yml')])
        serialized=json.dumps(plays)
        self.assertIn('recover-unstarted',serialized)
        self.assertNotIn('reset-lab-to-ovs.yml',serialized)
        self.assertNotIn('playbooks/00-bootstrap.yml',serialized)


if __name__=='__main__':unittest.main()
