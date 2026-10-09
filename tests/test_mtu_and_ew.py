"""Offline effective-MTU, placement, EW identity and exclusion regressions."""
import copy
import json
import pathlib
import struct
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import yaml
from test_validation import ROOT, v
from test_three_pairs import Scenario
import mtu_plan as m
import workload_resources as r
import validation_prerequisites as prerequisites


def inputs(mtu=1450):
    source=m.config_values(f'[DEFAULT]\nglobal_physnet_mtu={mtu}\n',
                          f'[ml2]\npath_mtu={mtu}\nmechanism_drivers=openvswitch\ntenant_network_types=vxlan\n')
    return dict(source_configs={'controller':source}, geneve_max_header_size=38,
                underlay={h:dict(interface='eth1',mtu=mtu,ipv4=['10.0.0.1'])
                          for h in ('network1','network2','compute1','compute2')})


class MtuTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=pathlib.Path(self.tmp.name)
        self.plan=m.calculate(inputs()); v.save(self.root/'mtu-calculation.json',self.plan)

    def networks(self, sizes=(1400,1360)):
        rows={f'net{i}':NS(id=f'net{i}',name=f'network-{i}',mtu=mtu,provider_network_type='vxlan') for i,mtu in enumerate(sizes)}
        network=Mock(); network.networks.side_effect=lambda:list(rows.values())
        network.get_network.side_effect=lambda uuid:rows[uuid]
        network.update_network.side_effect=lambda uuid,mtu:setattr(rows[uuid],'mtu',mtu)
        return NS(network=network),rows

    def target(self):
        return {'controller':dict(self.plan['inputs']['source_configs']['controller'],
            geneve_max_header_size=38,mechanism_drivers='ovn',tenant_network_types='geneve')}

    def test_confirmed_lab_uses_1400_to_1392(self):
        self.assertEqual(self.plan['validation_source_mtu'],1400)
        self.assertEqual(self.plan['validation_target_mtu'],1392)
        self.assertEqual(self.plan['fresh_geneve_mtu'],1392)
        self.assertEqual(self.plan['overhead_delta'],8)
        self.assertEqual(self.plan['underlay_limit'],1450)
        self.assertIn('minus IPv4',self.plan['reasoning'])

    def test_limits_come_from_effective_inputs_not_validation_constants(self):
        for underlay,source,target in ((1500,1450,1442),(9000,8950,8942)):
            with self.subTest(underlay=underlay):
                plan=m.calculate(inputs(underlay))
                self.assertEqual((plan['validation_source_mtu'],plan['fresh_geneve_mtu']),(source,target))

    def test_positive_path_mtu_and_global_physnet_limit(self):
        evidence=inputs(1500); evidence['source_configs']['controller']['path_mtu']=1450
        self.assertEqual(m.calculate(evidence)['fresh_geneve_mtu'],1392)
        evidence['source_configs']['controller'].update(global_physnet_mtu=1450,path_mtu=0)
        self.assertEqual(m.calculate(evidence)['fresh_geneve_mtu'],1392)

    def test_unknown_underlay_ipv4_or_inconsistent_controllers_fail(self):
        cases=[]
        x=inputs(); x['underlay']['compute1']['ipv4']=[]; cases.append(x)
        x=inputs(); x['underlay']['compute1']['mtu']=1400; cases.append(x)
        x=inputs(); x['source_configs']['other']=dict(x['source_configs']['controller'],path_mtu=1500); cases.append(x)
        x=inputs(); x['source_configs']['controller']['overlay_ip_version']=6; cases.append(x)
        for evidence in cases:
            with self.subTest(evidence=evidence),self.assertRaises(RuntimeError): m.calculate(evidence)

    def test_installed_template_header_is_parsed_without_guessing(self):
        self.assertEqual(m.template_header('[ml2_type_geneve]\nmax_header_size = 38\n[ovn]\n'),38)
        for text in ('','[ml2_type_geneve]\nmax_header_size={{ header }}\n',
                     '[ml2_type_geneve]\nmax_header_size=38\nmax_header_size=30\n'):
            with self.subTest(text=text),self.assertRaises(RuntimeError): m.template_header(text)

    def test_each_existing_network_has_its_own_mtu_journal(self):
        cloud,rows=self.networks(); result=m.prepare_networks(cloud,self.root)
        self.assertEqual([(r['source_mtu'],r['target_mtu']) for r in result],[(1400,1392),(1360,1352)])
        self.assertEqual(m.journal_rows(self.root/'network-mtu-migration.tsv'),{'net0':(1400,1392),'net1':(1360,1352)})
        m.prepare_networks(cloud,self.root)
        self.assertEqual(cloud.network.update_network.call_count,2)
        self.assertEqual(rows['net0'].mtu,1392)

    def test_journal_exists_before_lost_update_response_and_retry_does_not_reduce_twice(self):
        cloud,rows=self.networks()
        def lost(uuid,mtu):
            self.assertEqual(m.journal_rows(self.root/'network-mtu-migration.tsv')['net0'],(1400,1392))
            rows[uuid].mtu=mtu
            raise OSError('lost Neutron reply')
        cloud.network.update_network.side_effect=lost
        with self.assertRaises(OSError): m.prepare_networks(cloud,self.root)
        cloud.network.update_network.side_effect=lambda uuid,mtu:setattr(rows[uuid],'mtu',mtu)
        m.prepare_networks(cloud,self.root)
        self.assertEqual(rows['net0'].mtu,1392); self.assertEqual(rows['net1'].mtu,1352)

    def test_historical_tsv_is_read_without_rebasing_original(self):
        cloud,rows=self.networks((1392,1352))
        journal=self.root/'network-mtu-migration.tsv'; journal.write_text('net0\t1400\t1392\nnet1\t1360\t1352\n')
        original=journal.read_bytes(); m.prepare_networks(cloud,self.root)
        self.assertEqual(journal.read_bytes(),original); cloud.network.update_network.assert_not_called()

    def test_drift_or_ambiguous_journal_prevents_all_updates(self):
        cloud,_=self.networks()
        for text in ('net0 1400 1442\n','net0 1400 1392\nnet0 1400 1392\n','malformed\n','net0 1392 1384\n'):
            with self.subTest(text=text):
                (self.root/'network-mtu-migration.tsv').write_text(text)
                with self.assertRaises(RuntimeError): m.prepare_networks(cloud,self.root)
                cloud.network.update_network.assert_not_called()

    def test_generated_target_match_and_every_input_mismatch(self):
        m.verify_target(self.root,self.target())
        self.assertEqual(v.read_evidence(self.root,'mtu-target-config-verification.json')['status'],'PASS')
        for field,value in (('global_physnet_mtu',1500),('path_mtu',1500),('overlay_ip_version',6),
                            ('geneve_max_header_size',30),('mechanism_drivers','openvswitch'),('tenant_network_types','vxlan')):
            with self.subTest(field=field):
                changed=self.target(); changed['controller'][field]=value
                with self.assertRaises(RuntimeError): m.verify_target(self.root,changed)
                self.assertEqual(v.read_evidence(self.root,'mtu-target-config-verification.json')['status'],'FAIL')
        with self.assertRaises(RuntimeError): m.verify_target(self.root,{})

    def test_new_run_missing_or_failed_target_verification_cannot_enter_freeze(self):
        v.save(self.root/'runtime.json',{'mtu_plan_schema_version':1})
        with self.assertRaisesRegex(RuntimeError,'freeze prohibited'): m.require_pre_freeze(self.root)
        v.save(self.root/'mtu-target-configs.json',self.target()); m.verify_target(self.root,self.target())
        # Phase 06 proof alone is insufficient: Phase 07 must collect again.
        with self.assertRaisesRegex(RuntimeError,'freeze prohibited'): m.require_pre_freeze(self.root)
        intent=m.begin_pre_freeze_collection(self.root)
        fresh=dict(collection_id=intent['collection_id'],controllers={
            'controller':dict(controller='controller',collection_id=intent['collection_id'],
                              collected_at=intent['requested_at'],settings=self.target()['controller'])})
        v.save(self.root/'mtu-pre-freeze-target-configs.json',fresh)
        m.require_pre_freeze(self.root)
        fresh['controllers']['controller']['settings']['path_mtu']=1500
        v.save(self.root/'mtu-pre-freeze-target-configs.json',fresh)
        with self.assertRaises(RuntimeError): m.require_pre_freeze(self.root)

    def test_legacy_run_is_not_upgraded_or_required_to_have_new_mtu_artifacts(self):
        v.save(self.root/'runtime.json',{'phase_marker_schema_version':2})
        before=(self.root/'runtime.json').read_bytes(); m.require_pre_freeze(self.root)
        self.assertEqual((self.root/'runtime.json').read_bytes(),before)
        self.assertFalse((self.root/'mtu-target-config-verification.json').exists())

    def test_pre_freeze_verification_covers_enabled_and_disabled_guests(self):
        plays=yaml.safe_load((ROOT/'playbooks/07-migrate-db.yml').read_text())
        tasks=[t for play in plays for t in play['tasks']]
        freeze=next(i for i,t in enumerate(tasks) if '--freeze-start' in t.get('ansible.builtin.command',{}).get('argv',[]))
        checks=[t for t in tasks[:freeze] if 'ready' in t.get('ansible.builtin.command',{}).get('argv',[])]
        self.assertEqual(len(checks),1)
        self.assertNotIn('when',checks[0])
        self.assertEqual(plays[1]['hosts'],'control')
        self.assertTrue(all(play['any_errors_fatal'] for play in plays[:3]))

    def test_generated_endpoint_facts_still_belong_to_localhost(self):
        plays=yaml.safe_load((ROOT/'playbooks/06-target-config.yml').read_text())
        task=next(t for play in plays for t in play['tasks'] if t['name']=='Publish generated OVN endpoints to deployment host facts')
        self.assertEqual(task['delegate_to'],'localhost'); self.assertTrue(task['delegate_facts'])

    def test_source_and_target_config_reader_match_the_calculation_parser(self):
        from jinja2 import Template
        task=yaml.safe_load((ROOT/'playbooks/mtu-config-read-tasks.yml').read_text())[0]
        neutron='[DEFAULT]\nglobal_physnet_mtu=1450\n'
        ml2='[ml2]\npath_mtu=1450\nmechanism_drivers=ovn\ntenant_network_types=geneve\n[ml2_type_geneve]\nmax_header_size=38\n'
        (self.root/'neutron.conf').write_text(neutron); (self.root/'ml2.ini').write_text(ml2)
        for mode in ('source','target'):
            with self.subTest(mode=mode):
                shell=Template(task['ansible.builtin.shell']).render(mtu_config_mode=mode)
                code=shell.split("<<'PYCODE'\n",1)[1].rsplit('\nPYCODE',1)[0]
                for path in ('/etc/neutron/neutron.conf','/etc/kolla/neutron-server/neutron.conf'):
                    code=code.replace(path,str(self.root/'neutron.conf'))
                for path in ('/etc/neutron/plugins/ml2/ml2_conf.ini','/etc/kolla/neutron-server/ml2_conf.ini'):
                    code=code.replace(path,str(self.root/'ml2.ini'))
                output=subprocess.check_output([sys.executable,'-c',code],text=True)
                self.assertEqual(json.loads(output),m.config_values(neutron,ml2))


class PlacementAndSizingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=pathlib.Path(self.tmp.name); self.s=Scenario(self.root)
        self.obj=self.s.obj; self.obj.cfg.update(placement_required=True,compute_hosts={'fresh':['compute1','compute2']})
        for role in ('measure','existing','fresh'):
            for key in ('0','1'): self.s.servers[role+key].compute_host='compute'+str(int(key)+1)
        for net in ('pre-net0','pre-net1'): self.s.networks[net].provider_network_type='geneve'

    def test_pair_c_correct_actual_placement_is_checkpointed_and_reused(self):
        self.obj.create('post'); before=copy.deepcopy(self.obj.state['post']['fresh'])
        self.obj.create('post')
        self.assertEqual(self.obj.state['post']['fresh'],before)
        for key,vm in before.items(): self.assertEqual(vm['actual_host'],vm['expected_host'])
        self.obj.cloud.compute.create_server.assert_not_called()

    def test_incorrect_placement_preserves_vm_and_never_replaces_it(self):
        self.s.servers['fresh0'].compute_host='compute2'
        with self.assertRaisesRegex(RuntimeError,'placement'): self.obj.create('post')
        vm=self.obj.state['post']['fresh']['0']
        self.assertEqual((vm['expected_host'],vm['actual_host']),('compute1','compute2'))
        with self.assertRaises(RuntimeError): self.obj.create('post')
        self.obj.cloud.compute.create_server.assert_not_called(); self.obj.cloud.compute.delete_server.assert_not_called()

    def test_missing_actual_host_fails_in_new_runs(self):
        del self.s.servers['fresh0'].compute_host
        with self.assertRaisesRegex(RuntimeError,'placement'): self.obj.create('post')

    def test_resume_rejects_changed_host_or_server_port_ip(self):
        for fault in ('host','server','port','fixed_ips'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as d:
                s=Scenario(pathlib.Path(d)); obj=s.obj
                for net in ('pre-net0','pre-net1'): s.networks[net].provider_network_type='geneve'
                obj.cfg.update(placement_required=True,compute_hosts={'fresh':['compute1','compute2']})
                for key in ('0','1'): s.servers['fresh'+key].compute_host='compute'+str(int(key)+1)
                obj.create('post')
                if fault=='host': s.servers['fresh0'].compute_host='compute2'
                elif fault=='server': s.servers['fresh0'].id='different'
                elif fault=='port': s.ports['fresh-port0'].id='different'
                else: s.ports['fresh-port0'].fixed_ips=[]
                with self.assertRaises(RuntimeError): obj.create('post')
                obj.cloud.compute.create_server.assert_not_called(); obj.cloud.network.create_port.assert_not_called()

    def test_new_pair_c_request_and_uuid_are_checkpointed_before_wait(self):
        for vm in self.obj.state['post']['fresh'].values(): vm.pop('server')
        self.obj.cloud.compute.servers.return_value=[]
        def create(**kw):
            key=kw['name'][-1]; expected='compute'+str(int(key)+1)
            self.assertEqual(kw['availability_zone'],'nova:'+expected)
            checkpoint=v.read_evidence(self.root,'validation-resources.json')['post']['fresh'][key]
            self.assertEqual(checkpoint['expected_host'],expected)
            server=NS(id='new'+key,name=kw['name'],status='BUILD',compute_host=expected,metadata=kw['metadata'])
            self.s.servers[server.id]=server
            self.s.ports[kw['networks'][0]['port']].device_id=server.id
            return server
        def wait(server,**kw):
            self.assertEqual(v.read_evidence(self.root,'validation-resources.json')['post']['fresh'][server.id[-1]]['server'],server.id)
            server.status='ACTIVE'; return server
        self.obj.cloud.compute.create_server.side_effect=create
        self.obj.cloud.compute.wait_for_server.side_effect=wait
        self.obj.create('post'); self.obj.create('post')
        self.assertEqual(self.obj.cloud.compute.create_server.call_count,2)

    def test_pair_a_b_actual_hosts_are_recorded_without_forcing_placement(self):
        for net in ('pre-net0','pre-net1'): self.s.networks[net].provider_network_type='vxlan'
        for stage in ('measure','pre'):
            self.obj.create(stage)
            for vm in self.obj.pair(stage).values():
                self.assertIsNone(vm['expected_host']); self.assertTrue(vm['actual_host'].startswith('compute'))

    def test_changed_host_also_fails_post_validation_without_masking_identity(self):
        self.obj.create('post'); self.s.servers['fresh0'].compute_host='compute2'
        with patch.object(v.time,'sleep'),patch.object(v.time,'monotonic',side_effect=self.s.monotonic):
            with self.assertRaises(RuntimeError): self.obj.wait('post')
        checks=v.read_evidence(self.root,'post-workload-checks.json')
        self.assertEqual(checks['0']['placement'],'FAIL'); self.assertEqual(checks['0']['identity'],'FAIL')

    def test_new_validation_networks_request_their_own_source_and_fresh_limits(self):
        self.obj.cfg.update(mtu_schema_version=1,source_mtu=1400,target_mtu=1392)
        for key in ('0','1'):
            self.s.networks['pre-net'+key].provider_network_type='vxlan'
            self.s.networks['pre-net'+key].mtu=1400
            self.s.networks['post-net'+key].mtu=1392
            self.obj.state['pre']['networks'][key].pop('network')
            self.obj.state['post']['networks'][key].pop('network')
        def create(name,mtu):
            stage='pre' if '-pre-' in name else 'post'; key=name[-1]
            net=self.s.networks[stage+'-net'+key]; net.mtu=mtu; return net
        self.obj.cloud.network.create_network.side_effect=create
        self.obj.create('measure'); self.obj.create('pre')
        self.assertEqual([c.kwargs['mtu'] for c in self.obj.cloud.network.create_network.call_args_list],[1400,1400])
        for key in ('0','1'):
            self.s.networks['pre-net'+key].provider_network_type='geneve'
            self.s.networks['pre-net'+key].mtu=1392
        self.obj.create('post')
        self.assertEqual([c.kwargs['mtu'] for c in self.obj.cloud.network.create_network.call_args_list],[1400,1400,1392,1392])
        self.assertEqual(self.obj.pair('pre')['0']['source_mtu'],1400)
        self.assertEqual(self.obj.pair('pre')['0']['target_mtu'],1392)
        self.assertEqual(self.obj.pair('post')['0']['source_mtu'],1392)

    def test_image_min_disk_virtual_size_and_ram_are_checked(self):
        flavor=NS(disk=8,ram=1024)
        for image in (NS(min_disk=10),NS(virtual_size=10*1024**3),NS(min_ram=2048),NS(properties={'virtual_size':str(10*1024**3)})):
            with self.subTest(image=image),self.assertRaisesRegex(RuntimeError,'does not fit'):
                r.image_flavor_compatibility(image,flavor)
        result=r.image_flavor_compatibility(NS(min_disk=10,virtual_size=10*1024**3),NS(disk=10,ram=2048))
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(r.image_flavor_compatibility(NS(),flavor)['virtual_size_availability'],'UNAVAILABLE')

    def test_incompatible_image_fails_before_validation_resource_creation(self):
        self.obj.cloud.image.find_image.return_value=NS(id='netfix',min_disk=10)
        with self.assertRaises(RuntimeError): self.obj.create('post')
        self.obj.cloud.compute.create_server.assert_not_called(); self.obj.cloud.network.create_network.assert_not_called()

    def test_qcow_header_virtual_size_is_not_compressed_file_size(self):
        path=self.root/'image.qcow2'; data=bytearray(32); data[:4]=b'QFI\xfb'; data[24:32]=struct.pack('!Q',10*1024**3)
        path.write_bytes(data)
        self.assertEqual(prerequisites.qcow_virtual_size(path),10*1024**3)
        path.write_bytes(b'bad')
        with self.assertRaises(RuntimeError): prerequisites.qcow_virtual_size(path)

    def test_netfix_min_disk_cannot_reuse_managed_eight_gb_flavor(self):
        from test_prerequisites import PrerequisiteTests
        run=self.root/'run'; run.mkdir()
        cfg,cloud,image,_=PrerequisiteTests().setup_cloud(self.root)
        cfg['image']='ew-ubuntu-24.04-netfix'; image.name=cfg['image']; image.min_disk=10
        with self.assertRaisesRegex(RuntimeError,'does not fit'): prerequisites.prepare(cloud,cfg,run)
        cloud.compute.create_flavor.assert_not_called(); cloud.compute.create_server.assert_not_called()

    def test_new_mtu_contract_has_no_legacy_default_and_supports_per_network_values(self):
        self.obj.cfg.update(mtu_schema_version=1,source_mtu=1400,target_mtu=1392)
        vm={'source_mtu':1360,'target_mtu':1352}
        self.assertEqual(self.obj.expected_mtu(vm),1352)
        self.assertEqual(self.obj.expected_mtu(vm,source=True),1360)
        with self.assertRaises(RuntimeError): self.obj.expected_mtu({})
        self.obj.cfg.pop('mtu_schema_version')
        self.assertEqual(self.obj.expected_mtu({}),1392)


class ExistingEwTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=pathlib.Path(self.tmp.name)
        self.cfg=yaml.safe_load((ROOT/'group_vars/all.yml').read_text())['ew_workload_config']
        self.cloud=NS(compute=Mock(),network=Mock(),image=Mock())
        self.image=NS(id='ew-image',name=self.cfg['image'],min_disk=10,virtual_size=10*1024**3)
        self.flavor=NS(id='ew-flavor',name=self.cfg['flavor'],vcpus=2,ram=2048,disk=10)
        self.router=NS(id='ew-router-uuid',name=self.cfg['router'],external_gateway_info=None)
        self.servers={}; self.ports={}; self.nets={}
        for name in ('ew-net-a','ew-net-b'):
            self.nets[name]=NS(id=name+'-uuid',name=name,provider_network_type='vxlan',is_router_external=False)
        for row in self.cfg['servers']:
            self.servers[row['name']]=NS(id=row['name']+'-uuid',name=row['name'],status='ACTIVE',
                compute_host=row['compute_host'],image={'id':'ew-image'},flavor={'original_name':self.cfg['flavor']})
            self.ports[row['name']+'-uuid']=NS(id=row['name']+'-port',device_id=row['name']+'-uuid',
                fixed_ips=[{'subnet_id':row['network']+'-subnet','ip_address':row['ip']}],security_group_ids=['ew-sg'])
        self.cloud.image.images.return_value=[self.image]; self.cloud.compute.flavors.return_value=[self.flavor]
        self.cloud.network.routers.return_value=[self.router]
        self.cloud.network.networks.side_effect=lambda name:[self.nets[name]]
        self.cloud.compute.servers.side_effect=lambda name,**kw:[self.servers[name]]
        self.cloud.compute.get_server.side_effect=lambda uuid:next(s for s in self.servers.values() if s.id==uuid)
        self.cloud.network.ports.side_effect=self.port_list

    def port_list(self,device_id,network_id):
        if device_id==self.router.id:
            return [NS(fixed_ips=[{'subnet_id':network_id.removesuffix('-uuid')+'-subnet','ip_address':'192.168.0.1'}])]
        return [self.ports[device_id]]

    def test_exact_six_resource_catalog_is_non_owned_and_idempotent(self):
        state=r.resolve_ew(self.cloud,self.cfg,self.root)
        self.assertEqual(len(state['servers']),6)
        self.assertTrue(all(vm['owned'] is False for vm in state['servers'].values()))
        original=(self.root/'ew-resources.json').read_bytes()
        r.resolve_ew(self.cloud,self.cfg,self.root)
        self.assertEqual(original,(self.root/'ew-resources.json').read_bytes())
        self.cloud.compute.create_server.assert_not_called(); self.cloud.network.update_network.assert_not_called()

    def test_duplicate_name_or_missing_expected_port_fails_without_checkpoint(self):
        self.cloud.compute.servers.side_effect=lambda name,**kw:[self.servers[name],self.servers[name]]
        with self.assertRaisesRegex(RuntimeError,'exactly one'): r.resolve_ew(self.cloud,self.cfg,self.root)
        self.assertFalse((self.root/'ew-resources.json').exists())
        self.cloud.compute.servers.side_effect=lambda name,**kw:[self.servers[name]]
        self.ports['ew-app-uuid'].fixed_ips=[]
        with self.assertRaisesRegex(RuntimeError,'exact expected'): r.resolve_ew(self.cloud,self.cfg,self.root)

    def test_changed_existing_uuid_does_not_rebase_checkpoint(self):
        r.resolve_ew(self.cloud,self.cfg,self.root); original=(self.root/'ew-resources.json').read_bytes()
        self.nets['ew-net-a'].id='changed-uuid'
        with self.assertRaises(RuntimeError): r.resolve_ew(self.cloud,self.cfg,self.root)
        self.assertEqual(original,(self.root/'ew-resources.json').read_bytes())

    def test_external_gateway_or_wrong_host_fails(self):
        self.router.external_gateway_info={'network_id':'external'}
        with self.assertRaises(RuntimeError): r.resolve_ew(self.cloud,self.cfg,self.root)
        self.router.external_gateway_info=None; self.servers['ew-app'].compute_host='compute2'
        with self.assertRaises(RuntimeError): r.resolve_ew(self.cloud,self.cfg,self.root)

    def test_ew_server_port_network_subnet_router_and_sg_are_protected(self):
        state=r.resolve_ew(self.cloud,self.cfg,self.root)
        ids=[('server','ew-app-uuid'),('port','ew-app-port'),('network','ew-net-a-uuid'),
             ('subnet','ew-net-a-subnet'),('router',state['router']),('security_group','ew-sg')]
        for kind,uuid in ids:
            with self.subTest(kind=kind),self.assertRaisesRegex(RuntimeError,'protected'):
                r.assert_not_ew(self.root,{},kind,uuid)
        r.assert_not_ew(self.root,{},'server','unrelated-owned-uuid')

    def test_reboot_and_cleanup_cannot_target_catalogued_ew_resources(self):
        r.resolve_ew(self.cloud,self.cfg,self.root)
        for operation in ('pre-reboot','post-reboot','cleanup-server','cleanup-network','cleanup-port'):
            with self.subTest(operation=operation):
                scenario=Scenario(self.root); obj=scenario.obj
                obj.cfg.update(ew_workloads_enabled=True,ew_workload_config=self.cfg,allow_pre_cutover_guest_reboot=True,
                               allow_post_cutover_guest_reboot=True)
                vm=obj.pair('pre')['0']; entry={k:vm[k] for k in ('server','port','fixed_ips')}
                if operation=='cleanup-network': obj.state['pre']['networks']['0']['network']='ew-net-a-uuid'
                elif operation=='cleanup-port': vm['port']=entry['port']='ew-app-port'
                else: vm['server']=entry['server']='ew-app-uuid'
                with self.assertRaisesRegex(RuntimeError,'protected'):
                    if operation=='pre-reboot': obj.assert_reboot_owner('0',entry)
                    elif operation=='post-reboot': obj.post_owner('0',entry,request=True)
                    else: obj.cleanup('pre')
                obj.cloud.compute.reboot_server.assert_not_called(); obj.cloud.compute.delete_server.assert_not_called()
                obj.cloud.network.delete_network.assert_not_called()

    def test_configured_ew_name_is_protected_even_without_catalog_activation(self):
        with self.assertRaisesRegex(RuntimeError,'protected'):
            r.assert_not_ew(self.root,{'ew_workload_config':self.cfg},'server','some-uuid','ew-app')
        with self.assertRaisesRegex(RuntimeError,'Missing protected'):
            r.assert_not_ew(self.root,{'ew_workloads_enabled':True},'server','some-uuid')

    def test_validation_creation_cannot_attach_to_existing_ew_resources(self):
        r.resolve_ew(self.cloud,self.cfg,self.root)
        s=Scenario(self.root); s.obj.state['pre']['router']='ew-router-uuid'
        with self.assertRaisesRegex(RuntimeError,'protected'): s.obj.create('measure')
        s.obj.cloud.network.add_interface_to_router.assert_not_called()
        s.obj.cloud.compute.create_server.assert_not_called()

    def test_cleanup_rejects_live_ew_network_name_when_catalog_discovery_is_disabled(self):
        s=Scenario(self.root); s.obj.cfg['ew_workload_config']=self.cfg
        s.obj.cloud.network.find_network.return_value=NS(name='ew-net-a')
        with self.assertRaisesRegex(RuntimeError,'protected'): s.obj.cleanup('pre')
        s.obj.cloud.compute.delete_server.assert_not_called()
        s.obj.cloud.network.delete_network.assert_not_called()

    def test_duplicate_configured_server_does_not_silently_reduce_topology(self):
        bad=copy.deepcopy(self.cfg); bad['servers'][1]=bad['servers'][0]
        with self.assertRaisesRegex(RuntimeError,'six distinct'): r.resolve_ew(self.cloud,bad,self.root)

    def test_config_and_catalog_contain_no_secret_values_or_owned_alias(self):
        state=r.resolve_ew(self.cloud,self.cfg,self.root)
        raw=json.dumps(state).lower()
        for secret in ('password','token','private_key','baseline_output'): self.assertNotIn(secret,raw)
        self.assertNotIn('validation-resources.json',raw)


if __name__=='__main__': unittest.main()
