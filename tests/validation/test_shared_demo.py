"""Reset-created demo, shared external admission, and non-destructive coexistence."""
import base64
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import yaml
from tests.validation.test_validation import ROOT
from tests.validation.test_ns_lifecycle import MemoryNetwork
from openstack.compute.v2.server import Server
from openstack.compute.v2.service import Service
from openstack.compute.v2.hypervisor import Hypervisor
from openstack.network.v2.agent import Agent
from dataplane_capture import save
from reset_demo import Demo, validate_config, existing_ids
from shared_external import select, require_free_ips
from source_topology import before, verify
from extended_validation import Extended


def configuration():
    return dict(enabled=True, prefix='ovn-demo', cidrs=['10.234.0.0/24','10.234.1.0/24'],
        compute_hosts=['compute1','compute2'],router_ip='10.1.0.6',floating_ip='10.1.0.7',
        console_user='demo',console_password='',source_mtu=1400,endpoint_ip='10.1.0.187',timeout=2,
        workload_cidrs=['10.231.0.0/24','10.231.1.0/24','10.232.0.0/24','10.232.1.0/24','10.233.0.0/24'],
        external=dict(network='ovn-shared-external',subnet='ovn-shared-external-subnet',
            cidr='10.1.0.0/24',gateway_ip='10.1.0.1',physnet='physnet1',mtu=1450,
            pool_start='10.1.0.4',pool_end='10.1.0.10',snat_ip='10.1.0.4',fip_ip='10.1.0.5'))


class NetworkStore(MemoryNetwork):
    def create(self, kind, **attrs):
        if kind == 'ip':
            network = self.data['port'][attrs['port_id']].network_id
            attrs['router_id'] = next(r.id for r in self.data['router'].values() if
                any(p.network_id==network and p.device_id==r.id for p in self.data['port'].values()))
        obj = super().create(kind, **attrs)
        if kind == 'ip':
            # The base test store defaults to its first router. Model the actual
            # associated tenant router and Neutron's floating-IP service port.
            obj.router_id = attrs['router_id']
            sub = next(s for s in self.data['subnet'].values() if s.network_id==obj.floating_network_id)
            p = super().create('port',network_id=obj.floating_network_id,
                fixed_ips=[dict(subnet_id=sub.id,ip_address=obj.floating_ip_address)])
            p.device_owner, p.device_id = 'network:floatingip', obj.id
        return obj

    def __getattr__(self, name):
        if name.startswith('delete_'):
            kind = name[7:]
            def delete(identifier, ignore_missing=False):
                self.writes.append(('delete',kind,identifier))
                self.data[kind].pop(identifier,None)
                if kind in ('ip','router'):
                    for key,p in list(self.data['port'].items()):
                        if p.device_id==identifier:self.data['port'].pop(key)
            return delete
        return super().__getattr__(name)

    def security_group_rules(self, **query):
        return [r for r in self.rules if all(getattr(r,k)==v for k,v in query.items())]

    def remove_interface_from_router(self, router, subnet_id):
        for key,p in list(self.data['port'].items()):
            if p.device_id==router and any(f['subnet_id']==subnet_id for f in p.fixed_ips):
                self.data['port'].pop(key)


class DemoFixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'reset-one';self.root.mkdir()
        self.cfg=configuration();self.net=NetworkStore();self.servers={};self.user_data={}
        self.compute=Mock()
        self.compute.services.return_value=[Service(host=h,zone='nova',status='enabled',state='up') for h in self.cfg['compute_hosts']]
        self.compute.hypervisors.return_value=[Hypervisor(hypervisor_hostname='hv-'+h,service={'host':h},status='enabled',state='up') for h in self.cfg['compute_hosts']]
        self.net.agents=lambda **_: [Agent(host=h,alive=True,admin_state_up=True) for h in self.cfg['compute_hosts']]
        self.compute.servers.side_effect=lambda **q:[s for s in self.servers.values() if not q.get('name') or s.name==q['name']]
        self.compute.get_server.side_effect=lambda key:self.servers[key]
        self.compute.find_server.side_effect=lambda key:self.servers.get(key)
        def delete(key):
            self.servers.pop(key,None)
            for p in self.net.data['port'].values():
                if p.device_id==key:p.device_id=''
        self.compute.delete_server.side_effect=delete
        def console(key,**_):
            metadata=self.servers[key].metadata
            return dict(output='OVN_DEMO_READY_'+metadata['reset_demo_generation']+'_'+metadata['reset_demo_role'])
        self.compute.get_server_console_output.side_effect=console
        self.cloud=NS(network=self.net,compute=self.compute)
        def create(compute, target, **attrs):
            identifier='server-'+str(len(self.servers)+1)
            server=Server(id=identifier,name=attrs['name'],metadata=attrs['metadata'],status='ACTIVE',
                compute_host=target['nova_host'],hypervisor_hostname=target['hypervisor'])
            self.servers[identifier]=server;self.user_data[identifier]=base64.b64decode(attrs['user_data']).decode()
            port=self.net.get_port(attrs['networks'][0]['port'])
            port.device_id=identifier;port.device_owner='compute:nova';port.binding_host_id=target['neutron_host']
            return server
        self.create=create
        self.creator=patch('reset_demo.create_on_target',side_effect=create).start()
        self.addCleanup(patch.stopall)
        self.demo=Demo(self.cloud,self.root,self.cfg,dict(image={'id':'image'},flavor={'id':'flavor'}))

    def prepared(self):
        return self.demo.prepare()

    def source(self):
        root=Path(self.temp.name)/'run-one';root.mkdir(exist_ok=True)
        cfg=dict(self.cfg['external'],mtu_delta=8,reserve_ns_ips=True)
        save(root/'source-topology-config.json',cfg)
        return root


class ResetDemoTests(DemoFixture):
    def test_reset_builds_shared_network_two_computes_console_only_and_one_fip(self):
        result=self.prepared()
        self.assertEqual(len(self.servers),2)
        self.assertEqual({v['compute'] for v in result['vms']},{'compute1','compute2'})
        self.assertEqual(len(self.net.data['network']),3)
        self.assertEqual(len(self.net.data['subnet']),3)
        self.assertEqual(len(self.net.data['router']),1)
        self.assertEqual(result['floating_ip'],'10.1.0.7')
        self.assertFalse(result['migration_owned'])
        self.assertFalse((self.root/'validation-resources.json').exists())
        creds=json.loads((self.root/'demo-console-credentials.json').read_text())
        self.assertEqual((self.root/'demo-console-credentials.json').stat().st_mode & 0o777,0o600)
        self.assertNotIn(creds['password'],json.dumps(result))
        for content in self.user_data.values():
            self.assertNotIn(creds['password'],content)
            self.assertNotIn('migration-probe',content)
            self.assertNotIn('ns-probe',content)
            self.assertIn('hashed_passwd: $6$',content)
        self.assertTrue(all('ovn_migration_run' not in s.metadata for s in self.servers.values()))

    def test_repeated_prepare_keeps_exact_resources_password_and_placement(self):
        first=self.prepared();writes=len(self.net.writes)
        password=(self.root/'demo-console-credentials.json').read_bytes()
        second=self.prepared()
        self.assertEqual(first,second);self.assertEqual(len(self.net.writes),writes)
        self.assertEqual(self.creator.call_count,2)
        self.assertEqual((self.root/'demo-console-credentials.json').read_bytes(),password)

    def test_lost_server_response_recovers_only_matching_name_metadata_and_port(self):
        fired=False
        def lost(*args,**kwargs):
            nonlocal fired
            server=self.create(*args,**kwargs)
            if not fired:
                fired=True;raise RuntimeError('Nova reply lost')
            return server
        self.creator.side_effect=lost
        with self.assertRaisesRegex(RuntimeError,'reply lost'):self.prepared()
        self.prepared()
        self.assertEqual(len(self.servers),2)
        self.assertEqual(self.creator.call_count,2)

    def test_lost_fip_response_is_recovered_without_duplicate_or_reassignment(self):
        original=self.net.create_ip
        def lost(**attrs):
            original(**attrs);raise RuntimeError('FIP reply lost')
        with patch.object(self.net,'create_ip',side_effect=lost):
            with self.assertRaisesRegex(RuntimeError,'reply lost'):self.prepared()
        self.prepared()
        self.assertEqual(len(self.net.data['ip']),1)

    def test_foreign_same_name_is_never_adopted(self):
        self.net.create_network(name='ovn-shared-external',description='foreign')
        with self.assertRaisesRegex(RuntimeError,'another resource'):self.prepared()
        self.assertEqual(len(self.net.data['network']),1)

    def test_wrong_bound_compute_never_marks_demo_ready(self):
        self.compute.get_server_console_output.return_value={'output':''}
        self.compute.get_server_console_output.side_effect=None
        self.demo.wait=Mock(side_effect=TimeoutError('not ready'))
        with self.assertRaises(TimeoutError):self.prepared()
        self.assertFalse((self.root/'demo-resources.json').exists())

    def test_configuration_changes_and_unrelated_reset_remnants_are_rejected(self):
        self.prepared()
        changed=copy.deepcopy(self.cfg);changed['floating_ip']='10.1.0.8'
        with self.assertRaisesRegex(RuntimeError,'configuration changed'):
            Demo(self.cloud,self.root,changed,dict(image={'id':'image'},flavor={'id':'flavor'}))
        self.net.create_network(name='foreign',description='unowned')
        with self.assertRaisesRegex(RuntimeError,'Unexpected resource'):existing_ids(self.cloud,self.root)

    def test_changed_image_or_flavor_cannot_reuse_existing_demo(self):
        with self.assertRaisesRegex(RuntimeError,'image/flavor'):
            Demo(self.cloud,self.root,self.cfg,dict(image={'id':'replacement'},flavor={'id':'flavor'}))

    def test_reset_completion_requires_exact_demo_and_shared_external_resources(self):
        import reset_validation as reset
        from tests.reset.test_reset_four_nodes import ResetCloudTests
        save(self.root/'reset-demo-config.json',self.cfg)
        save(self.root/'validation-prerequisites-config.json',dict(source_mtu=1400,target_geneve_mtu=1392))
        result=dict(image=dict(id='image',name='image-name'),flavor=dict(id='flavor',name='flavor-name'))
        def cli(*args):
            if args[:2]==('image','show'):return dict(id='image')
            if args[1:2]==('list',) and args[0] in ('network','subnet','router'):
                return [dict(ID=obj.id) for obj in self.net.data[args[0]].values()]
            if args[:2]==('server','list'):return [dict(ID=key) for key in self.servers]
            return ResetCloudTests().cloud(*args)
        with patch.object(reset,'cloud_json',side_effect=cli), patch.object(reset.prerequisites,'prepare',return_value=result), patch.object(reset,'verify_image'):
            reset.finish(self.root,self.cloud)
        completed=json.loads((self.root/'reset-complete.json').read_text())
        self.assertEqual(completed['status'],'OVS_4NODE_BASELINE_READY')
        self.assertEqual(completed['demo']['status'],'READY')
        self.assertEqual(len(completed['demo']['vms']),2)
        # Even same-generation extra resources cannot be hidden by the final gate.
        self.net.create_network(name='extra',description='ovn-reset-generation='+self.root.name)
        with patch.object(reset,'cloud_json',side_effect=cli):
            with self.assertRaisesRegex(RuntimeError,'resource set differs'):
                reset.validate_cloud(completed['demo']['expected_resources'])

    def test_unsafe_ip_cidr_and_credential_settings_rejected_before_resource_writes(self):
        cases=[('floating_ip','10.1.0.4'),('router_ip','10.1.0.15'),('cidrs',['10.231.0.0/24','10.234.1.0/24']),
            ('cidrs',['10.234.0.0/30','10.234.1.0/24']),('console_user','demo;bad'),
            ('console_password','bad\npassword'),('source_mtu',1440)]
        for key,value in cases:
            with self.subTest(key=key,value=value):
                cfg=copy.deepcopy(self.cfg);cfg[key]=value
                with self.assertRaises((ValueError,TypeError)):validate_config(cfg)
        self.assertEqual(self.net.writes,[])


class SharedCoexistenceTests(DemoFixture):
    def test_precheck_admits_demo_but_preserves_read_only_ownership(self):
        self.prepared();root=self.source();writes=len(self.net.writes)
        result=before(self.cloud,root)
        self.assertEqual(result['mode'],'shared')
        self.assertFalse(result['shared_external']['owned'])
        self.assertEqual(len(result['servers']),2)
        self.assertEqual(len(self.net.writes),writes)

    def test_precheck_refuses_ip_reservation_collision_before_any_writes(self):
        self.prepared();root=self.source()
        ext=self.demo.state['external_network'];sub=self.demo.state['external_subnet']
        self.net.create_port(network_id=ext,fixed_ips=[dict(subnet_id=sub,ip_address='10.1.0.4')])
        writes=len(self.net.writes)
        with self.assertRaisesRegex(RuntimeError,'another owner'):before(self.cloud,root)
        self.assertEqual(len(self.net.writes),writes)
        self.assertFalse((root/'source-topology-before.json').exists())

    def test_precheck_still_rejects_other_provider_external_and_gateway_scope(self):
        self.prepared();root=self.source()
        for key,value in (('provider_network_type','vlan'),('provider_physical_network','wrong'),('mtu',1400)):
            with self.subTest(key=key):
                ext=self.net.get_network(self.demo.state['external_network']);old=getattr(ext,key);setattr(ext,key,value)
                with self.assertRaises(RuntimeError):before(self.cloud,root)
                setattr(ext,key,old)
        foreign=self.net.create_network(name='foreign-provider',provider_network_type='vlan')
        with self.assertRaisesRegex(RuntimeError,'Only VXLAN'):before(self.cloud,root)
        self.net.data['network'].pop(foreign.id)
        self.net.get_router(self.demo.state['router']).external_gateway_info={'network_id':'other-external'}
        with self.assertRaisesRegex(RuntimeError,'unsupported external'):before(self.cloud,root)

    def test_provider_vm_and_ha_router_remain_outside_supported_scope(self):
        self.prepared();root=self.source()
        port=self.net.create_port(network_id=self.demo.state['external_network'])
        port.device_owner='compute:nova'
        with self.assertRaisesRegex(RuntimeError,'provider/external VM'):before(self.cloud,root)
        self.net.data['port'].pop(port.id)
        router=self.net.get_router(self.demo.state['router']);router.is_ha=True
        with self.assertRaisesRegex(RuntimeError,'HA/distributed'):before(self.cloud,root)

    def test_preservation_allows_only_expected_conversion_and_detects_missing_vm_or_fip_changes(self):
        self.prepared();root=self.source();baseline=before(self.cloud,root)
        for n in self.net.data['network'].values():
            if n.provider_network_type=='vxlan':n.provider_network_type='geneve';n.mtu-=8
        self.assertEqual(verify(self.cloud,root,'after-migration')['status'],'PASS')
        fip=self.net.get_ip(self.demo.state['floating_ip']);old=fip.port_id;fip.port_id='changed'
        with self.assertRaisesRegex(RuntimeError,'FIP association'):verify(self.cloud,root,'after-workload-cleanup')
        self.assertEqual(json.loads((root/'preexisting-resource-preservation.json').read_text())['status'],'FAIL')
        fip.port_id=old
        key=baseline['servers'][0]['id'];server=self.servers.pop(key)
        with self.assertRaises(RuntimeError):verify(self.cloud,root,'after-workload-cleanup')
        self.servers[key]=server

    def automatic(self):
        root=Path(self.temp.name)/'run-one';root.mkdir(exist_ok=True)
        shared=select(self.net,dict(self.cfg['external'],mtu_delta=8))
        ns=dict(enabled=True,shared_external=shared,endpoint_ip='10.1.0.187',port=18180,
            external_cidr='10.1.0.0/24',external_gateway='10.1.0.1',physnet='physnet1',mtu=1450,
            snat_ip='10.1.0.4',fip_ip='10.1.0.5',tenant_cidr='10.233.0.0/24',outbound_ip='10.233.0.11',inbound_ip='10.233.0.20')
        cfg=dict(run='run-one',prefix='validation',ns=ns,timeout=2,lifetime=30,tcp_interval=.5,tcp_timeout=1,
            placement=self.demo.state['placement'],image='image',flavor='flavor')
        v=NS(root=root,cfg=cfg,cloud=self.cloud,wait_active=Mock(),wait_placement=Mock(),placement=Mock(return_value={'status':'PASS'}))
        x=Extended(v)
        patcher=patch('validation_placement.create_on_target',side_effect=self.create);patcher.start()
        self.addCleanup(patcher.stop)
        def local(role,action,cfg):
            if role=='snat-server':(root/'ns/snat-server.jsonl').write_text('ready')
        x.local_process=Mock(side_effect=local);x.console_rows=Mock(return_value=[{'kind':'server'}])
        return x

    def test_automatic_workload_shares_external_and_cleanup_retains_every_demo_resource(self):
        demo=self.prepared();source=self.source();before(self.cloud,source)
        x=self.automatic();x.create_ns();x.ns_snapshot()
        self.assertFalse(x.state['ns']['external_owned'])
        self.assertEqual(len(self.net.data['network']),4)
        self.assertEqual(len(self.net.data['ip']),2)
        x.create_ns()  # Same run resumes against its exact existing allocations.
        x.cleanup()
        self.assertEqual(set(self.servers),{v['id'] for v in demo['vms']})
        self.assertEqual(len(self.net.data['network']),3)
        self.assertEqual(set(self.net.data['ip']),{demo['floating_ip_id']})
        self.assertIn(self.demo.state['external_subnet'],self.net.data['subnet'])
        self.assertEqual(x.state['ns']['external_network'],demo['external']['network']['id'])
        for n in self.net.data['network'].values():
            if n.provider_network_type=='vxlan':n.provider_network_type='geneve';n.mtu-=8
        self.assertEqual(verify(self.cloud,source,'after-workload-cleanup')['status'],'PASS')

    def test_shared_checkpoint_change_blocks_cleanup_before_any_delete(self):
        self.prepared();x=self.automatic();x.create_ns()
        x.cfg['ns']['shared_external']['network']['id']='another'
        writes=len(self.net.writes);ids=set(self.servers)
        with self.assertRaises((RuntimeError,KeyError)):x.cleanup()
        self.assertEqual(set(self.servers),ids);self.assertEqual(len(self.net.writes),writes)

    def test_automatic_lost_fip_reply_recovers_on_shared_external(self):
        self.prepared();x=self.automatic();original=self.net.create_ip
        def lost(**attrs):original(**attrs);raise RuntimeError('lost FIP reply')
        with patch.object(self.net,'create_ip',side_effect=lost):
            with self.assertRaisesRegex(RuntimeError,'lost FIP'):x.create_ns()
        x.create_ns();self.assertEqual(len(self.net.data['ip']),2)


class SharedAnsibleTests(unittest.TestCase):
    def test_production_reset_settings_render_before_destroy_and_do_not_log_password(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);snapshot=root/'reset-one';snapshot.mkdir()
            (root/'scripts').symlink_to(ROOT/'scripts',target_is_directory=True)
            taskfile=ROOT/'playbooks/tasks/reset-demo-preflight-tasks.yml'
            harness=root/'test.yml'
            harness.write_text(yaml.safe_dump([dict(hosts='localhost',gather_facts=False,
                vars_files=[str(ROOT/'group_vars/reset.yml'),str(ROOT/'group_vars/ns-lab.yml')],
                vars=dict(reset_run_dir=str(snapshot),reset_demo_console_password='private-test-password'),
                tasks=[{'ansible.builtin.include_tasks':str(taskfile)}])],sort_keys=False))
            extra=root/'extra.json';extra.write_text(json.dumps(dict(reset_demo_console_password='private-test-password')))
            proc=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i','localhost,',str(harness),'-c','local','-e','@'+str(extra)],
                capture_output=True,text=True,timeout=30)
            self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
            self.assertNotIn('private-test-password',proc.stdout+proc.stderr)
            path=snapshot/'reset-demo-config.json';cfg=json.loads(path.read_text())
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertEqual(cfg['console_password'],'private-test-password')
            self.assertEqual(cfg['external']['snat_ip'],'10.1.0.4')
            self.assertEqual(cfg['router_ip'],'10.1.0.6')
            validate_config(cfg)

    def test_guest_config_retains_all_automatic_workloads_and_shared_checkpoint(self):
        plays=yaml.safe_load((ROOT/'playbooks/04-validation-workloads.yml').read_text())
        task=next(t for p in plays for t in p['tasks'] if t['name']=='[04] [SAVE] Save workload validation settings')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            shared=dict(owned=False,network={'id':'shared-network'},subnet={'id':'shared-subnet'})
            save(root/'source-topology-before.json',dict(shared_external=shared))
            inventory=root/'inventory';inventory.write_text('[network]\nnetwork1\n[compute]\ncompute1\ncompute2\n')
            harness=root/'test.yml'
            harness.write_text(yaml.safe_dump([dict(hosts='localhost',gather_facts=False,
                vars_files=[str(ROOT/'group_vars/all.yml'),str(ROOT/'group_vars/ns-lab.yml')],
                vars=dict(migration_run_dir=str(root),migration_run_id='run-one',kolla_inventory_runtime=str(inventory),
                    ovn_cli_host_runtime='controller',validation_prerequisites_runtime=dict(image={'id':'image'},flavor={'id':'flavor'}),
                    validation_placement_runtime={'0':{},'1':{}}),tasks=[task])],sort_keys=False))
            proc=subprocess.run([sys.executable,'-m','ansible.cli.playbook','-i',str(inventory),str(harness)],
                capture_output=True,text=True,timeout=30)
            self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
            cfg=json.loads((root/'validation-config.json').read_text())
            self.assertTrue(cfg['ns']['enabled']);self.assertTrue(cfg['pair_d_enabled']);self.assertTrue(cfg['ew_matrix_enabled'])
            self.assertEqual(cfg['ns']['shared_external'],shared)
            self.assertEqual(cfg['ns']['snat_ip'],'10.1.0.4')
            self.assertNotIn('reset_demo_generation',json.dumps(cfg))

    def test_precheck_and_preservation_guards_cover_demo_and_resumed_run(self):
        pre=(ROOT/'playbooks/02-precheck.yml').read_text()
        self.assertNotIn('reset before owned N-S test',pre)
        self.assertIn('source_topology.py',pre)
        report=yaml.safe_load((ROOT/'playbooks/13-report.yml').read_text())[0]['tasks']
        names=[t['name'] for t in report]
        cleanup=names.index('[13] Finalize and clean up owned workloads when checks pass')
        check=names.index('[13] Check demo and shared external resources survive cleanup')
        rebuild=names.index('[13] Update reports with workload cleanup results')
        self.assertLess(cleanup,check);self.assertLess(check,rebuild)
        self.assertIn('source-topology-before.json',str(report[check]['when']))
        source=(ROOT/'scripts/reporting/report_format.py').read_text().partition('\ndef terminal_summary(')[0]
        import hashlib
        self.assertEqual(hashlib.sha256(source.encode()).hexdigest(),'3ca3e49f1507dfdf18bcfbfed48139051d98b194580be06be16344e588a62157')

    def test_missing_baseline_cannot_use_a_stale_preservation_pass(self):
        from tests.validation.test_validation import ready_evidence
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'metrics').mkdir()
            save(root/'runtime.json',dict(phase_marker_schema_version=2,preexisting_preservation_enabled=True))
            ready_evidence(root)
            for name in ('pre-cleanup.json','post-cleanup.json'):save(root/name,dict(status='PASS'))
            save(root/'preexisting-resource-preservation.json',dict(status='PASS'))
            proc=subprocess.run([sys.executable,str(ROOT/'scripts/reporting/migration_report.py'),str(root),'test-run','inventory'],capture_output=True,text=True,timeout=15)
            self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
            data=json.loads((root/'migration-report.json').read_text())
            self.assertEqual(data['result'],'MIGRATED_VALIDATION_INCOMPLETE')
            self.assertEqual(data['preexisting_resources_preservation']['status'],'UNAVAILABLE')

    def test_preservation_failure_marks_json_incomplete_without_changing_txt(self):
        from tests.validation.test_validation import ready_evidence
        texts=[]
        for outcome in ('PASS','FAIL','UNAVAILABLE'):
            with self.subTest(outcome=outcome),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);(root/'metrics').mkdir()
                save(root/'runtime.json',dict(phase_marker_schema_version=2))
                ready_evidence(root)
                for name in ('pre-cleanup.json','post-cleanup.json'):save(root/name,dict(status='PASS'))
                save(root/'source-topology-before.json',dict(schema=1))
                save(root/'preexisting-resource-preservation.json',dict(status=outcome,owned=False))
                proc=subprocess.run([sys.executable,str(ROOT/'scripts/reporting/migration_report.py'),str(root),'test-run','inventory'],
                    capture_output=True,text=True,timeout=15)
                self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
                report=json.loads((root/'migration-report.json').read_text())
                self.assertEqual(report['result'],'SUCCESS' if outcome=='PASS' else 'MIGRATED_VALIDATION_INCOMPLETE')
                self.assertEqual(report['preexisting_resources_preservation']['status'],outcome)
                texts.append((root/'migration-report.txt').read_text())
        self.assertEqual(len(set(texts)),1)


if __name__=='__main__':unittest.main()
