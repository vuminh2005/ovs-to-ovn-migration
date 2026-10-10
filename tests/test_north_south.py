"""Work Item 3 offline boundaries. No cloud/SSH/service operations are executed."""
import base64
import copy
import hashlib
import contextlib
import io
import configparser
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import uuid
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import yaml
from test_validation import ROOT, ready_evidence
from dataplane_capture import save
import ns_scope as scope
import ns_host as host
import ns_ovn as ovn
import ns_probe as probe
import ns_probe_agent as agent
import ns_measurement as measurement
import ns_workflow as workflow
import network_semantics
import mtu_plan
from test_mtu_and_ew import inputs
from ew_tcp_experiment import restoration_markers


def uid(n): return str(uuid.UUID(int=n))


def config(ingress=False):
    cfg=dict(schema_version=1,network=uid(1),subnet=uid(2),router=uid(3),gateway_port=uid(4),
        type='flat',segmentation_id=None,physnet='physnet1',mtu=1450,cidr='198.51.100.0/24',gateway='198.51.100.1',
        allocation_pools=[dict(start='198.51.100.100',end='198.51.100.199')],snat=True,
        gateway_hosts=['network1','network2'],hosts=['network1','network2','compute1','compute2'],
        gateways={h:dict(chassis_hostname=h,bridge='br-ex',uplink='external0',uplink_mac='02:00:00:00:00:01',uplink_mtu=1450,upstream_routes_verified=True,nested_forwarding_verified=True) for h in ('network1','network2')},
        interval=.2,timeout=1.,stable_samples=3,lifetime=300,readiness_timeout=10,
        egress=dict(guest=dict(server=uid(5),port=uid(6),network=uid(7),subnet=uid(8),ip='192.0.2.11',mac='fa:16:3e:00:00:11',compute='compute1'),
                    probe=dict(address='203.0.113.20',port=18090,path='/probe',endpoint_id='controlled-external')),ingress=None,
        transport=dict(transport_timeout=10,inventory='fixture',namespace_hosts=['network1'],host_known_hosts='/private/management-trust',guest_known_hosts='/private/guest-trust'))
    if ingress:
        cfg['ingress']=dict(fip=uid(15),floating_ip='198.51.100.100',
            guest=dict(server=uid(16),port=uid(17),network=uid(7),subnet=uid(8),ip='192.0.2.12',mac='fa:16:3e:00:00:12',compute='compute2'),
            observer=dict(address='probe-management',user='root',key='/private/key',known_hosts='/private/trust',product_uuid=uid(18),boot=uid(19),ip='203.0.113.21'),
            probe=dict(address='198.51.100.100',port=18091,path='/probe',endpoint_id='controlled-tenant'))
    return cfg


def cloud(cfg):
    # Actual SDK resources, with Neutron wire attribute names.
    from openstack.network.v2.network import Network
    from openstack.network.v2.port import Port
    from openstack.network.v2.subnet import Subnet
    from openstack.network.v2.router import Router
    from openstack.network.v2.floating_ip import FloatingIP
    from openstack.compute.v2.server import Server
    networks=[Network(id=cfg['network'],**{'router:external':True,'provider:network_type':cfg['type'],
                'provider:physical_network':cfg['physnet'],'provider:segmentation_id':cfg['segmentation_id'],'mtu':1450,'subnets':[cfg['subnet']]}),
              Network(id=uid(7),**{'router:external':False,'provider:network_type':'vxlan','mtu':1400})]
    subnets={cfg['subnet']:Subnet(id=cfg['subnet'],network_id=cfg['network'],cidr=cfg['cidr'],gateway_ip=cfg['gateway'],allocation_pools=cfg['allocation_pools'],ip_version=4),
             uid(8):Subnet(id=uid(8),network_id=uid(7),cidr='192.0.2.0/24',ip_version=4)}
    gw=[dict(subnet_id=cfg['subnet'],ip_address='198.51.100.101')]
    routers=[Router(id=cfg['router'],distributed=False,ha=False,routes=[],external_gateway_info=dict(network_id=cfg['network'],enable_snat=True,external_fixed_ips=gw))]
    ports=[Port(id=cfg['gateway_port'],network_id=cfg['network'],device_id=cfg['router'],device_owner='network:router_gateway',mac_address='fa:16:3e:00:01:01',fixed_ips=gw),
           Port(id=uid(9),network_id=uid(7),device_id=cfg['router'],device_owner='network:router_interface',mac_address='fa:16:3e:00:02:01',fixed_ips=[dict(subnet_id=uid(8),ip_address='192.0.2.1')])]
    servers={}
    for d in ('egress','ingress'):
        if not cfg.get(d): continue
        vm=cfg[d]['guest']
        ports.append(Port(id=vm['port'],network_id=vm['network'],device_id=vm['server'],device_owner='compute:nova',mac_address=vm['mac'],fixed_ips=[dict(subnet_id=vm['subnet'],ip_address=vm['ip'])],status='ACTIVE',**{'binding:host_id':vm['compute'],'binding:vnic_type':'normal','binding:vif_type':'ovs'}))
        servers[vm['server']]=Server(id=vm['server'],status='ACTIVE',**{'OS-EXT-SRV-ATTR:host':vm['compute']})
    fips=[]
    if cfg.get('ingress'):
        f=cfg['ingress']; fips=[FloatingIP(id=f['fip'],floating_network_id=cfg['network'],router_id=cfg['router'],port_id=f['guest']['port'],fixed_ip_address=f['guest']['ip'],floating_ip_address=f['floating_ip'])]
    c=Mock(); c.network.networks.side_effect=lambda:iter(networks); c.network.routers.side_effect=lambda:iter(routers)
    c.network.ports.side_effect=lambda:iter(ports); c.network.ips.side_effect=lambda:iter(fips)
    c.network.floating_ip_port_forwardings.return_value=[]; c.network.get_subnet.side_effect=lambda id:subnets[id]; c.compute.get_server.side_effect=lambda id:servers[id]
    c.network.get_network.side_effect=lambda id:next(n for n in networks if n.id==id)
    return c,networks,ports,routers,fips,servers


def profile(vm,boot='guest-boot',mtu=1392):
    return dict(server=vm['server'],boot=boot,interfaces=[dict(ifname='ens3',mtu=mtu,address=vm['mac'],operstate='UP',
            addr_info=[dict(family='inet',local=vm['ip'])])],routes=[dict(dst='default',gateway='192.0.2.1')])


def raw_config():
    return dict(run='run',direction='egress',boot='boot',interval=.2,timeout=1.,stable_samples=3,probe=dict(address='203.0.113.20',port=18090,path='/probe',endpoint_id='controlled-external'))


def raw(cfg=None, failures=(4,5), count=12):
    cfg=cfg or raw_config(); rows=[]
    for seq in range(1,count+1):
        rows.append(dict(seq=seq,run=cfg['run'],direction=cfg['direction'],boot=cfg['boot'],mono=seq*.2,http_end=seq*.2+.01,end_mono=seq*.2+.02,http_success=seq not in failures,session=None))
    return dict(rows=rows,state=dict(config=cfg,boot=cfg['boot'],status='RUNNING',seq=count,last_mono=rows[-1]['end_mono']),current_mono=rows[-1]['end_mono']+.01)


def anchor(raw, seq):
    r=raw['rows'][seq-1]; return dict(seq=seq,mono=r['end_mono'],boot=r['boot'])


def host_evidence():
    def p(n,b,t='',ids=None,options=None):
        return dict(_uuid=n+'-port',name=n,bridge=b,external_ids={},interfaces=[dict(_uuid=n+'-interface',name=n,type=t,external_ids=ids or {},options=options or {})])
    return dict(ports=[p('external0','br-ex'),p('int-br-ex','br-int','patch',options={'peer':'phy-br-ex'}),p('phy-br-ex','br-ex','patch',options={'peer':'int-br-ex'}),
                       p('qg-exact','br-ex','internal',{'iface-id':uid(4)})],
        bridges=['br-int','br-ex'],external_ids={'hostname':'network1','system-id':'chassis1','ovn-bridge-mappings':'physnet1:br-ex','ovn-cms-options':'enable-chassis-as-gw'},
        links=[dict(ifname='external0',address='02:00:00:00:00:01',mtu=1450,flags=['UP'],addr_info=[])],
        namespaces={'qrouter-'+uid(3):[dict(ifname='lo'),dict(ifname='qg-exact',address='fa:16:3e:00:01:01')]},namespace_ids={'qrouter-'+uid(3):dict(inode=12,device=1)})


class ScopeTests(unittest.TestCase):
    def test_supported_flat_vlan_and_outbound_only(self):
        for ingress in (False,True):
            cfg=config(ingress); scope.validate_config(cfg,['network1','network2'],['compute1','compute2'])
            c,*_=cloud(cfg); s=scope.snapshot(c,cfg); self.assertEqual(s['network']['type'],'flat'); self.assertEqual(s['tenant_cidrs'],['192.0.2.0/24'])
        cfg=config(); cfg.update(type='vlan',segmentation_id=210)
        scope.validate_config(cfg,['network1','network2'],['compute1','compute2']); scope.snapshot(cloud(cfg)[0],cfg)

    def test_missing_unknown_and_ambiguous_inputs_fail_closed(self):
        for edit in (lambda c:c.update(network='not-a-uuid'),lambda c:c.update(type='geneve'),lambda c:c.update(segmentation_id=21),
                     lambda c:c.update(snat=False),lambda c:c.update(gateway_hosts=['compute1']),
                     lambda c:c['gateways']['network1'].update(nested_forwarding_verified=False)):
            cfg=config(); edit(cfg)
            with self.subTest(cfg=cfg),self.assertRaises((RuntimeError,ValueError)): scope.validate_config(cfg,['network1','network2'],['compute1','compute2'])
        cfg=config(); del cfg['gateway_port']
        with self.assertRaises(KeyError): scope.validate_config(cfg,['network1','network2'],['compute1','compute2'])

    def test_dvr_ha_unreviewed_external_and_direct_provider_guest_rejected(self):
        for kind in ('dvr','ha','provider','duplicate-gateway','unknown-fip'):
            cfg=config(); c,nets,ports,routers,fips,_=cloud(cfg)
            if kind=='dvr': routers[0].is_distributed=True
            if kind=='ha': routers[0].is_ha=True
            if kind=='provider': ports[-1].network_id=cfg['network']
            if kind=='duplicate-gateway': ports.append(ports[0])
            if kind=='unknown-fip': fips.append(NS(id=uid(99)))
            with self.subTest(kind=kind),self.assertRaises(RuntimeError): scope.snapshot(c,cfg)

    def test_exact_fip_gateway_and_identity_preservation(self):
        cfg=config(True); c,nets,ports,routers,fips,_=cloud(cfg); before=scope.snapshot(c,cfg)
        self.assertTrue(scope.preserved(before,copy.deepcopy(before)))
        for key in ('network','subnet','gateway','interfaces','fips','guests'):
            after=copy.deepcopy(before); after[key]=None
            with self.subTest(key=key),self.assertRaises(RuntimeError): scope.preserved(before,after)
        fips[0].port_id=uid(99)
        with self.assertRaisesRegex(RuntimeError,'FIP-to-fixed'): scope.snapshot(c,cfg)

    def test_target_geneve_keeps_external_mtu_and_semantics(self):
        cfg=config(); c,nets,*_=cloud(cfg); before=scope.snapshot(c,cfg)
        nets[1].provider_network_type='geneve'; nets[1].mtu=1392
        after=scope.snapshot(c,cfg,True); scope.preserved(before,after)
        row=dict(network_type_before='flat',segmentation_id_before=None,external=True,physical_network_before='physnet1',mtu_before=1450)
        self.assertEqual(network_semantics.audit(row,nets[0],{'neutron-'+cfg['network']},{'geneve'})['status'],'PASS')
        nets[0].mtu=1442
        self.assertEqual(network_semantics.audit(row,nets[0],{'neutron-'+cfg['network']},{'geneve'})['status'],'FAIL')

    def test_mtu_planner_never_updates_external_network(self):
        cfg=config(); c,nets,*_=cloud(cfg)
        def update(id,**kwargs): c.network.get_network(id).mtu=kwargs['mtu']
        c.network.update_network.side_effect=update
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); save(root/'mtu-calculation.json',mtu_plan.calculate(inputs()))
            rows=mtu_plan.prepare_networks(c,root)
            self.assertEqual([r['network'] for r in rows],[uid(7)]); self.assertEqual(nets[0].mtu,1450)
            c.network.update_network.assert_called_once_with(uid(7),mtu=1392)

    def test_kolla_does_not_enable_compute_provider_mappings(self):
        cfg=config(); values=dict(neutron_bridge_name='br-ex',neutron_external_interface='external0')
        workflow.check_kolla(cfg,values)
        for bad in (dict(values,enable_neutron_provider_networks=True),dict(values,neutron_bridge_name='other')):
            with self.assertRaises(RuntimeError): workflow.check_kolla(cfg,bad)


class ControllerGateTests(unittest.TestCase):
    def execute(self,cfg,mode='target',ml2=None,missing=False):
        task=yaml.safe_load((ROOT/'playbooks/ns-controller-tasks.yml').read_text())[0]
        code=task['ansible.builtin.shell'].split("<<'PYCODE'\n",1)[1].rsplit('\nPYCODE',1)[0]
        default='[ml2]\ntype_drivers=flat,vlan,vxlan,geneve\n[ml2_type_flat]\nflat_networks=physnet1\n[ml2_type_vlan]\nnetwork_vlan_ranges=physnet1:100:200\n'
        def read(parser,path):
            parser.read_string((ml2 if ml2 is not None else default) if str(path).endswith('ml2_conf.ini') else '[DEFAULT]\nglobal_physnet_mtu=1450\n')
        with patch.object(sys,'argv',['helper',json.dumps(cfg),mode]),patch.object(pathlib.Path,'is_file',return_value=not missing),patch.object(configparser.ConfigParser,'read',read),contextlib.redirect_stdout(io.StringIO()) as output:
            exec(compile(code,'ns-controller-tasks:embedded','exec'),{})
        return json.loads(output.getvalue())

    def test_source_and_fresh_generated_target_support_are_actually_parsed(self):
        for mode in ('source','target'):
            self.assertEqual(self.execute(config(),mode)['physical_mtu_limit'],1450)
        cfg=config(); cfg.update(type='vlan',segmentation_id=120)
        self.assertEqual(self.execute(cfg)['type'],'vlan')
        cfg['segmentation_id']=300
        with self.assertRaisesRegex(AssertionError,'VLAN'): self.execute(cfg)

    def test_missing_unreadable_or_changed_external_config_blocks_before_freeze(self):
        with self.assertRaisesRegex(AssertionError,'Missing'): self.execute(config(),missing=True)
        with self.assertRaisesRegex(AssertionError,'type driver'): self.execute(config(),ml2='[ml2]\ntype_drivers=geneve\n')
        cfg=config(); cfg['mtu']=1500
        with self.assertRaisesRegex(AssertionError,'MTU'): self.execute(cfg)

    def test_final_ready_refreshes_current_globals_even_with_saved_source_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); globals_path=root/'globals.yml'; globals_path.write_text('neutron_bridge_name: changed-after-genconfig\nneutron_external_interface: external0\n')
            save(root/'runtime.json',dict(globals=str(globals_path)))
            w=object.__new__(workflow.Workflow); w.root=root; w.cfg=config(); w.api_snapshot=Mock()
            with self.assertRaisesRegex(RuntimeError,'Kolla'): w.ready()
            w.api_snapshot.assert_not_called()


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.cfg=config(); self.snapshot=scope.snapshot(cloud(self.cfg)[0],self.cfg); self.evidence=host_evidence()

    def test_exact_qg_patch_retirement_preserves_uplink_ovn_and_unrelated(self):
        evidence=self.evidence
        ovn_port=copy.deepcopy(evidence['ports'][1]); ovn_port.update(name='patch-provnet',_uuid='ovn'); ovn_port['interfaces'][0]['external_ids']={'ovn-localnet-port':'localnet'}
        evidence['ports'].append(ovn_port); evidence['namespaces']['qrouter-'+uid(90)]=[]
        plan=host.cleanup_plan(self.cfg,self.snapshot,evidence)
        self.assertEqual({p['name'] for p in plan['ports']},{'qg-exact','int-br-ex','phy-br-ex'})
        self.assertEqual([p['name'] for p in plan['namespaces']],['qrouter-'+uid(3)])
        with patch.object(host.subprocess,'run',return_value=NS(returncode=0,stdout='[{"State":{"Running":false}}]')),patch.object(host,'command') as command:
            host.retire(plan,evidence,True)
        args=[a.args[0] for a in command.call_args_list]
        self.assertFalse(any('external0' in a or 'patch-provnet' in a or 'qrouter-'+uid(90) in a for a in args))

    def test_changed_uuid_mac_or_patch_owner_blocks_before_mutation(self):
        plan=host.cleanup_plan(self.cfg,self.snapshot,self.evidence)
        changed=copy.deepcopy(self.evidence); changed['ports'][3]['_uuid']='replacement'
        with patch.object(host.subprocess,'run',return_value=NS(returncode=0,stdout='[{"State":{"Running":false}}]')),patch.object(host,'command') as command,self.assertRaises(RuntimeError): host.retire(plan,changed)
        command.assert_not_called()
        self.evidence['ports'][3]['interfaces'][0]['external_ids']['iface-id']=uid(99)
        with self.assertRaises(RuntimeError): host.cleanup_plan(self.cfg,self.snapshot,self.evidence)

    def test_namespace_inode_reuse_blocks_all_mutation(self):
        plan=host.cleanup_plan(self.cfg,self.snapshot,self.evidence); self.evidence['namespace_ids']['qrouter-'+uid(3)]['inode']=99
        with patch.object(host.subprocess,'run',return_value=NS(returncode=0,stdout='[{"State":{"Running":false}}]')),patch.object(host,'command') as command,self.assertRaises(RuntimeError): host.retire(plan,self.evidence,True)
        command.assert_not_called()

    def test_new_namespace_interface_or_wrong_api_namespace_owner_blocks_deletion(self):
        plan=host.cleanup_plan(self.cfg,self.snapshot,self.evidence)
        changed=copy.deepcopy(self.evidence); changed['namespaces']['qrouter-'+uid(3)].append(dict(ifname='unrelated',address='02:00:00:00:00:99'))
        with patch.object(host.subprocess,'run',return_value=NS(returncode=0,stdout='[{"State":{"Running":false}}]')),patch.object(host,'command') as command,self.assertRaises(RuntimeError): host.retire(plan,changed,True)
        command.assert_not_called()
        self.snapshot['legacy_ports'][0]['device']=uid(99)
        with self.assertRaises(RuntimeError): host.cleanup_plan(self.cfg,self.snapshot,self.evidence)

    def test_retirement_is_idempotent_when_exact_artifacts_already_gone(self):
        plan=host.cleanup_plan(self.cfg,self.snapshot,self.evidence); self.evidence['ports']=self.evidence['ports'][:1]; self.evidence['namespaces']={}
        with patch.object(host.subprocess,'run',return_value=NS(returncode=0,stdout='[{"State":{"Running":false}}]')),patch.object(host,'command') as command:
            host.retire(plan,self.evidence,True)
        command.assert_not_called()

    def test_uplink_mapping_mtu_and_management_address_gates(self):
        host.verify_path(self.cfg,'network1',self.evidence,True)
        for key,value in (('mtu',1400),('addr_info',[{'local':'management-ip'}]),('flags',[])):
            evidence=copy.deepcopy(self.evidence); evidence['links'][0][key]=value
            with self.subTest(key=key),self.assertRaises(RuntimeError): host.verify_path(self.cfg,'network1',evidence,True)


class OVNTests(unittest.TestCase):
    def fixture(self,ingress=False):
        cfg=config(ingress); before=scope.snapshot(cloud(cfg)[0],cfg); gateway=before['gateway']; interface=before['interfaces'][0]
        hosts={h:host_evidence() for h in cfg['gateway_hosts']}; hosts['network2']['external_ids'].update({'system-id':'chassis2','hostname':'network2'})
        for name,bridge,peer in [('ovn-int','br-int','ovn-ext'),('ovn-ext','br-ex','ovn-int')]:
            hosts['network1']['ports'].append(dict(name=name,bridge=bridge,external_ids={'ovn-localnet-port':'localnet'},interfaces=[dict(type='patch',options={'peer':peer},external_ids={})]))
        db={'Logical_Router':[dict(_uuid='router',name='neutron-'+cfg['router'],ports=['ext','tenant'],nat=['snat']+(['fip'] if ingress else []),static_routes=['default'])],
            'Logical_Router_Port':[dict(_uuid='ext',name='lrp-'+cfg['gateway_port'],networks=['198.51.100.101/24'],mac=gateway['mac'],gateway_chassis=['gc1','gc2']),dict(_uuid='tenant',name='lrp-'+interface['id'],networks=['192.0.2.1/24'],mac=interface['mac'])],
            'NAT':[dict(_uuid='snat',type='snat',logical_ip='192.0.2.0/24',external_ip='198.51.100.101')],
            'Gateway_Chassis':[dict(_uuid='gc1',chassis_name='chassis1',priority=2),dict(_uuid='gc2',chassis_name='chassis2',priority=1)],
            'Chassis':[dict(_uuid='c1',name='chassis1',hostname='network1',other_config={'ovn-cms-options':'enable-chassis-as-gw'}),dict(_uuid='c2',name='chassis2',hostname='network2',other_config={'ovn-cms-options':'enable-chassis-as-gw'})],
            'Port_Binding':[dict(logical_port='cr-lrp-'+cfg['gateway_port'],type='chassisredirect',chassis='c1',options={'distributed-port':'lrp-'+cfg['gateway_port']}),dict(logical_port='localnet',type='localnet',chassis=[],options={}),dict(logical_port=cfg['gateway_port'],type='patch',chassis=[],options={'peer':'lrp-'+cfg['gateway_port']})],
            'Logical_Switch':[dict(name='neutron-'+cfg['network'],ports=['local','gateway-link'])],
            'Logical_Switch_Port':[dict(_uuid='local',name='localnet',type='localnet',options={'network_name':'physnet1'},tag=[]),dict(_uuid='gateway-link',name=cfg['gateway_port'],type='router',options={'router-port':'lrp-'+cfg['gateway_port']},tag=[])],
            'Logical_Router_Static_Route':[dict(_uuid='default',ip_prefix='0.0.0.0/0',nexthop=cfg['gateway'],output_port=[],external_ids={'neutron:is_ext_gw':'true','neutron:subnet_id':cfg['subnet']})]}
        if ingress:
            f=before['fips'][0]; db['NAT'].append(dict(_uuid='fip',type='dnat_and_snat',logical_ip=f['fixed_ip'],external_ip=f['floating_ip'],external_ids={'neutron:fip_id':f['id'],'neutron:fip_port_id':f['port']}))
        return cfg,before,db,hosts

    def test_localnet_does_not_require_vm_up_and_default_route_can_omit_output(self):
        self.assertEqual(ovn.verify(*self.fixture())['status'],'PASS')
        self.assertEqual(ovn.verify(*self.fixture(True))['status'],'PASS')

    def test_gateway_binding_nat_fip_and_localnet_failures(self):
        edits=[lambda d:d['Port_Binding'][0].update(chassis=[]),lambda d:d['Port_Binding'][0].update(type='patch'),
               lambda d:d['NAT'][0].update(external_ip='198.51.100.99'),lambda d:d['NAT'][1].update(logical_ip='192.0.2.99'),
               lambda d:d['Logical_Switch_Port'][0]['options'].update(network_name='other'),lambda d:d['Gateway_Chassis'].pop(),
               lambda d:d['Logical_Router_Static_Route'][0].update(nexthop='198.51.100.99')]
        for edit in edits:
            cfg,before,db,hosts=self.fixture(True); edit(db)
            with self.subTest(edit=edit),self.assertRaises(RuntimeError): ovn.verify(cfg,before,db,hosts)

    def test_missing_physical_ovn_patch_and_duplicate_gateway_chassis_fail(self):
        cfg,before,db,hosts=self.fixture()
        hosts['network1']['ports']=[p for p in hosts['network1']['ports'] if not p['name'].startswith('ovn-')]
        with self.assertRaisesRegex(RuntimeError,'localnet patch'): ovn.verify(cfg,before,db,hosts)
        cfg,before,db,hosts=self.fixture(); db['Chassis'].append(copy.deepcopy(db['Chassis'][0]))
        with self.assertRaises(RuntimeError): ovn.verify(cfg,before,db,hosts)

    def test_singleton_ovsdb_network_sets_and_vlan_tag(self):
        cfg,before,db,hosts=self.fixture()
        for p in db['Logical_Router_Port']: p['networks']=p['networks'][0]
        cfg.update(type='vlan',segmentation_id=120); db['Logical_Switch_Port'][0]['tag']=120
        self.assertEqual(ovn.verify(cfg,before,db,hosts)['status'],'PASS')

    def test_list_queries_have_no_empty_record_and_are_structured(self):
        import ovn_workload_evidence as evidence
        with patch.object(evidence.subprocess,'check_output',return_value='{"headings":["name"],"data":[]}') as run:
            evidence.query('ovn-nbctl','tcp:private','Logical_Router','name','',operation='list')
        self.assertEqual(run.call_args.args[0][-2:],['list','Logical_Router'])


class MetricTests(unittest.TestCase):
    def test_independent_observations_count_failures_and_longest_recovered_burst(self):
        r=raw(); m=measurement.metrics(r,raw_config(),anchor(r,2),anchor(r,12),8)
        self.assertEqual(m['status'],'PASS'); self.assertEqual((m['attempts'],m['successes'],m['failures']),(10,8,2))
        self.assertAlmostEqual(m['longest_recovered_outage_seconds'],.41)
        self.assertEqual(m['coverage'],'PASS')

    def test_missing_sequence_gap_cross_boot_nan_and_unrecovered_window_unavailable(self):
        for kind in ('gap','time-gap','boot','nan','no-end','pre-fence','unrecovered'):
            r=raw(); start=anchor(r,2); end=anchor(r,12)
            if kind=='gap': del r['rows'][4]
            if kind=='time-gap': r['rows'][4].update(mono=99,http_end=99.1,end_mono=99.2)
            if kind=='boot': r['rows'][4]['boot']='new-boot'
            if kind=='nan': r['rows'][4]['mono']=float('nan')
            if kind=='no-end': end=None
            if kind=='pre-fence': end=anchor(r,9)
            if kind=='unrecovered': r['rows'][-1]['http_success']=False
            with self.subTest(kind=kind):
                m=measurement.metrics(r,raw_config(),start,end,8); self.assertEqual(m['status'],'UNAVAILABLE'); self.assertIsNone(m['longest_recovered_outage_seconds'])

    def test_completed_window_ignores_and_reports_post_end_gap(self):
        r=raw(); start=anchor(r,2); end=anchor(r,12)
        original=measurement.metrics(r,raw_config(),start,end,8)
        extra=raw(count=13)['rows'][-1]
        for key in ('mono','http_end','end_mono'): extra[key]+=100
        r['rows'].append(extra); r['state'].update(seq=13,last_mono=extra['end_mono'])
        completed=measurement.metrics(r,raw_config(),start,end,8)
        self.assertEqual(completed['status'],'PASS')
        for key in ('attempts','successes','failures','failure_percent','sampled_outage_windows',
                    'longest_recovered_outage_seconds','latency_seconds','session','observed_samples'):
            self.assertEqual(completed[key],original[key],key)
        self.assertEqual(completed['outside_window_anomalies'],[dict(sequence=13,location='after_end',reason='unexplained observer gap')])

    def test_inside_and_start_transition_gaps_cannot_pass(self):
        for first_shifted in (3,5):
            r=raw(); start=anchor(r,2)
            for row in r['rows'][first_shifted-1:]:
                for key in ('mono','http_end','end_mono'): row[key]+=100
            r['state']['last_mono']=r['rows'][-1]['end_mono']
            with self.subTest(first_shifted=first_shifted):
                m=measurement.metrics(r,raw_config(),start,anchor(r,12),8)
                self.assertEqual(m['status'],'UNAVAILABLE'); self.assertIn('gap within',m['reason'])

    def test_outside_sequence_holes_are_diagnostic_but_ambiguous_order_is_refused(self):
        r=raw(count=14); start=anchor(r,2); end=anchor(r,12)
        del r['rows'][12]
        m=measurement.metrics(r,raw_config(),start,end,8)
        self.assertEqual(m['status'],'PASS')
        self.assertTrue(any('missing outside' in a['reason'] for a in m['outside_window_anomalies']))
        r['rows'].append(copy.deepcopy(r['rows'][-1]))
        self.assertEqual(measurement.metrics(r,raw_config(),start,end,8)['status'],'UNAVAILABLE')

    def test_http_validation_requires_nonce_endpoint_and_observed_snat_address(self):
        cfg=dict(source_ip='192.0.2.11',timeout=1,probe=raw_config()['probe'],expected_peer='198.51.100.101')
        for bad in (None,'peer','nonce','endpoint_id'):
            response=dict(peer='198.51.100.101',nonce='nonce',endpoint_id='controlled-external')
            if bad: response[bad]='wrong'
            conn=Mock(); conn.getresponse.return_value.status=200; conn.getresponse.return_value.read.return_value=json.dumps(response).encode()
            with patch.object(probe.uuid,'uuid4',return_value='nonce'),patch.object(probe.http.client,'HTTPConnection',return_value=conn):
                self.assertEqual(probe.http_attempt(cfg),bad is None)
            conn.close.assert_called_once()

    def test_optional_sessions_remain_distinct_from_fresh_requests(self):
        cfg=raw_config(); cfg['probe']['session_port']=18100; r=raw(cfg,failures=())
        for row in r['rows']: row['session']=dict(success=True,opened=row['seq'] in (1,6),reset=row['seq']==5)
        m=measurement.metrics(r,cfg,anchor(r,2),anchor(r,12),8)
        self.assertEqual(m['failures'],0); self.assertEqual(m['session']['resets'],1); self.assertEqual(m['session']['connections_opened'],1)

    def test_non_object_http_json_is_a_failed_observation(self):
        cfg=dict(source_ip='192.0.2.11',timeout=1,probe=raw_config()['probe'],expected_peer='198.51.100.101')
        for body in ([],None,123,'unexpected'):
            conn=Mock(); conn.getresponse.return_value.status=200
            conn.getresponse.return_value.read.return_value=json.dumps(body).encode()
            with self.subTest(body=body),patch.object(probe.http.client,'HTTPConnection',return_value=conn):
                self.assertFalse(probe.http_attempt(cfg))
            conn.close.assert_called_once()

    def test_open_failure_window_is_retained_when_no_end_anchor_exists(self):
        r=raw(failures=(10,11,12)); m=measurement.metrics(r,raw_config(),anchor(r,2),None,8)
        self.assertEqual(m['status'],'UNAVAILABLE')
        self.assertEqual(m['observed_samples']['failures'],3)
        self.assertIsNone(m['sampled_outage_windows'][-1]['recovery_monotonic'])
        self.assertIsNone(m['longest_recovered_outage_seconds'])

    def test_disabled_historical_and_enabled_missing_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); self.assertEqual(measurement.report(root)['status'],'NOT TESTED')
            save(root/'runtime.json',dict(ns_enabled=True)); self.assertEqual(measurement.report(root)['status'],'UNAVAILABLE')


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=pathlib.Path(self.tmp.name)
        self.cfg=raw_config(); self.cfg['helper_sha256']=hashlib.sha256(b'helper').hexdigest()
        self.payload=dict(action='start',config=self.cfg,source=base64.b64encode(b'helper').decode())
        self.patch=patch.object(agent,'BASE',self.root); self.patch.start(); self.addCleanup(self.patch.stop)
        self.base=self.root/'run/egress'

    def test_intent_precedes_launch_and_crash_never_duplicates(self):
        def launch(*args,**kwargs):
            self.assertTrue((self.base/'start-intent.json').is_file()); self.assertTrue((self.base/'config.json').is_file())
            raise OSError('mock crash')
        with patch.object(agent.subprocess,'Popen',side_effect=launch) as popen:
            with self.assertRaises(OSError): agent.operation(self.payload)
            with self.assertRaisesRegex(RuntimeError,'Ambiguous'): agent.operation(self.payload)
            self.assertEqual(popen.call_count,1)
        self.assertTrue((self.base/'runner.log').exists())

    def test_exact_live_process_reused_no_second_start_and_foreign_config_refused(self):
        self.base.mkdir(parents=True); (self.base/'ns_probe.py').write_bytes(b'helper'); save(self.base/'config.json',self.cfg); save(self.base/'start-intent.json',{})
        save(self.base/'state.json',dict(status='RUNNING',pid=23,boot='boot'))
        with patch.object(agent,'alive',return_value=True),patch.object(agent.subprocess,'Popen') as popen:
            self.assertEqual(agent.operation(self.payload)['pid'],23); popen.assert_not_called()
            self.payload['config']=dict(self.cfg,boot='changed')
            with self.assertRaises(RuntimeError): agent.operation(self.payload)

    def test_dead_runner_fail_closed_and_raw_evidence_retained(self):
        self.base.mkdir(parents=True); (self.base/'ns_probe.py').write_bytes(b'helper'); save(self.base/'config.json',self.cfg); save(self.base/'state.json',dict(status='RUNNING'))
        evidence=self.base/'events.jsonl'; evidence.write_text('raw evidence')
        with patch.object(agent,'alive',return_value=False),self.assertRaises(RuntimeError): agent.operation(dict(self.payload,action='collect'))
        self.assertEqual(evidence.read_text(),'raw evidence')

    def test_start_anchor_never_reset_on_reuse_and_no_cloud_calls_in_takeover(self):
        w=object.__new__(workflow.Workflow); w.root=self.root; w.cfg=config(); w.cloud=cloud(w.cfg)[0]; w.before=scope.snapshot(w.cloud,w.cfg)
        r=raw(); entry=dict(config=raw_config(),start=anchor(r,2)); w.life=dict(observers={'egress':entry})
        w.api_snapshot=Mock(return_value=copy.deepcopy(w.before)); w.hosts=Mock(return_value={}); w.profile=Mock(return_value=({'boot':'boot'},{})); w.call=Mock(return_value=r); w.commit=Mock()
        w.start(); self.assertEqual(entry['start'],anchor(r,2)); w.commit.assert_not_called()
        # All takeover calls use checkpointed host SSH/OVN DB endpoints, with cloud unavailable.
        cfg,before,db,hosts=OVNTests().fixture(); w.cfg=cfg; w.before=before; w.hosts=Mock(return_value=hosts)
        save(self.root/'runtime.json',dict(ovn_cli_host='controller')); (self.root/'ml2_conf.ini.target').write_text('[ovn]\novn_nb_connection=tcp:nb\novn_sb_connection=tcp:sb\n')
        w.transport=Mock(); w.transport.host_argv.return_value=['ssh','root@controller']; w.transport.run.return_value=json.dumps(db)
        w.cloud=Mock(); w.takeover(); self.assertFalse(w.cloud.mock_calls)

    def test_recovery_keeps_existing_fences_and_end_anchors_after_interruption(self):
        IntegrationTests().fixture(self.root)
        w=object.__new__(workflow.Workflow); w.root=self.root; w.cfg=config()
        w.life=json.loads((self.root/'ns-lifecycle.json').read_text()); original=copy.deepcopy(w.life)
        w.api_snapshot=Mock(return_value=json.loads((self.root/'ns-after.json').read_text())); w.takeover=Mock()
        w.profile=Mock(return_value=(profile(w.cfg['egress']['guest']),{})); w.call=Mock(return_value=json.loads((self.root/'ns-egress-raw.json').read_text())); w.commit=Mock()
        old=json.loads((self.root/'ns-recovery.json').read_text())
        w.recovery()
        self.assertEqual(w.life,original); self.assertEqual(json.loads((self.root/'ns-recovery.json').read_text())['fences'],old['fences'])
        w.commit.assert_not_called()

    def test_expired_observer_never_restarted_and_exact_stop_is_requested(self):
        self.base.mkdir(parents=True); (self.base/'ns_probe.py').write_bytes(b'helper'); save(self.base/'config.json',self.cfg); save(self.base/'start-intent.json',{})
        save(self.base/'state.json',dict(status='EXPIRED',pid=23))
        with patch.object(agent,'alive',return_value=False),patch.object(agent.subprocess,'Popen') as start:
            with self.assertRaises(RuntimeError): agent.operation(self.payload)
            start.assert_not_called()
        save(self.base/'state.json',dict(status='RUNNING',pid=23))
        (self.base/'events.jsonl').write_text('')
        def live(state,base):
            if (base/'stop').exists():
                save(base/'state.json',dict(status='STOPPED',seq=0)); return False
            return True
        with patch.object(agent,'alive',side_effect=live),patch.object(agent.subprocess,'Popen') as start:
            self.assertEqual(agent.operation(dict(self.payload,action='stop'))['state']['status'],'STOPPED')
            start.assert_not_called()

    def test_remote_process_identity_rejects_pid_reuse_wrong_boot_and_foreign_argv(self):
        state=dict(pid=42,boot='expected-boot',start_ticks='123')
        argv=[b'python3',str(self.base/'ns_probe.py').encode(),str(self.base).encode(),b'']
        def text(path,*a,**kw):
            return 'expected-boot' if str(path).endswith('boot_id') else '42 (python3) '+' '.join(['S']*19+['123'])
        with patch.object(pathlib.Path,'read_text',text),patch.object(pathlib.Path,'read_bytes',return_value=b'\0'.join(argv)):
            self.assertTrue(agent.alive(state,self.base))
            self.assertFalse(agent.alive(dict(state,start_ticks='other'),self.base))
            self.assertFalse(agent.alive(dict(state,boot='other'),self.base))
        with patch.object(pathlib.Path,'read_text',text),patch.object(pathlib.Path,'read_bytes',return_value=b'other\0command'):
            self.assertFalse(agent.alive(state,self.base))

    def test_failed_acceptance_never_stops_or_deletes_observers(self):
        w=object.__new__(workflow.Workflow); w.root=self.root; w.call=Mock(); w.life={'observers':{'egress':{}}}
        save(self.root/'migration-report.json',dict(result='MIGRATED_VALIDATION_INCOMPLETE'))
        with self.assertRaises(RuntimeError): w.finalize()
        w.call.assert_not_called()


class TakeoverRefreshTests(unittest.TestCase):
    def fixture(self,root):
        cfg,before,db,hosts=OVNTests().fixture()
        cfg['readiness_timeout']=.5
        w=object.__new__(workflow.Workflow); w.root=root; w.cfg=cfg; w.before=before
        w.cloud=Mock(); w.transport=Mock(); w.transport.deadline=None
        w.transport.host_argv.return_value=['ssh','root@controller']
        w.transport.run.return_value=json.dumps(db)
        save(root/'runtime.json',dict(ovn_cli_host='controller'))
        (root/'ml2_conf.ini.target').write_text('[ovn]\novn_nb_connection=tcp:nb\novn_sb_connection=tcp:sb\n')
        save(root/'ns-host-before.json',hosts)
        save(root/'ns-ovn-takeover.json',dict(status='PASS',old_evidence=True))
        return w,hosts

    @contextlib.contextmanager
    def clock(self):
        state=NS(now=0.,sleeps=[])
        def sleep(seconds): state.sleeps.append(seconds); state.now+=seconds
        with patch.object(workflow.time,'monotonic',side_effect=lambda:state.now),patch.object(workflow.time,'sleep',side_effect=sleep):
            yield state

    def missing_patch(self,hosts):
        absent=copy.deepcopy(hosts)
        absent['network1']['ports']=[p for p in absent['network1']['ports'] if not p['name'].startswith('ovn-')]
        return absent

    def test_later_fresh_host_path_converges_and_used_evidence_is_persisted(self):
        with tempfile.TemporaryDirectory() as d,self.clock() as clock:
            root=pathlib.Path(d); w,hosts=self.fixture(root)
            absent=self.missing_patch(hosts); w.hosts=Mock(side_effect=[absent,hosts])
            w.takeover()
            self.assertEqual(w.hosts.call_count,2); self.assertEqual(w.transport.run.call_count,2)
            first=json.loads((root/'ns-takeover-attempt-0001.json').read_text())
            second=json.loads((root/'ns-takeover-attempt-0002.json').read_text())
            self.assertEqual(first['hosts'],absent); self.assertEqual(first['status'],'FAIL')
            self.assertEqual(second['hosts'],hosts); self.assertEqual(second['status'],'PASS')
            self.assertEqual(first['ovn'],second['ovn'])
            self.assertEqual(json.loads((root/'ns-ovn-takeover.json').read_text())['evidence_file'],'ns-takeover-attempt-0002.json')
            self.assertEqual(clock.sleeps,[.2]); self.assertIsNone(w.transport.deadline)
            self.assertFalse(w.cloud.mock_calls)
            w.hosts.return_value=hosts; w.hosts.side_effect=None
            w.takeover()
            self.assertTrue((root/'ns-takeover-attempt-0003.json').exists())
            self.assertEqual(json.loads((root/'ns-takeover-attempt-0001.json').read_text()),first)

    def test_never_appearing_path_times_out_without_reusing_old_pass(self):
        with tempfile.TemporaryDirectory() as d,self.clock() as clock:
            root=pathlib.Path(d); w,hosts=self.fixture(root); w.hosts=Mock(return_value=self.missing_patch(hosts))
            with self.assertRaisesRegex(RuntimeError,'timed out'): w.takeover()
            self.assertAlmostEqual(clock.now,.5); self.assertEqual(w.hosts.call_count,3)
            final=json.loads((root/'ns-ovn-takeover.json').read_text())
            self.assertEqual(final['status'],'FAIL'); self.assertTrue(final['timed_out'])
            for path in root.glob('ns-takeover-attempt-*.json'):
                self.assertEqual(json.loads(path.read_text())['status'],'FAIL')

    def test_host_collection_timeout_is_bounded_and_authentication_is_not_retried(self):
        for error,retry in [(subprocess.TimeoutExpired('mock-host-read',.1),True),(RuntimeError('SSH authentication/host-key prerequisite'),False)]:
            with self.subTest(error=type(error).__name__),tempfile.TemporaryDirectory() as d,self.clock() as clock:
                root=pathlib.Path(d); w,hosts=self.fixture(root); w.hosts=Mock(side_effect=[error,hosts])
                if retry: w.takeover(); self.assertEqual(w.hosts.call_count,2)
                else:
                    with self.assertRaises(RuntimeError): w.takeover()
                    self.assertEqual(w.hosts.call_count,1); self.assertFalse(clock.sleeps)
                    w.transport.run.assert_not_called()
                first=json.loads((root/'ns-takeover-attempt-0001.json').read_text())
                self.assertEqual(first['stage'],'hosts'); self.assertEqual(first['retryable'],retry)

    def test_changed_uplink_wrong_gateway_and_wrong_patch_refuse_immediately(self):
        for kind in ('uplink','gateway','patch'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as d,self.clock() as clock:
                root=pathlib.Path(d); w,hosts=self.fixture(root); changed=copy.deepcopy(hosts)
                if kind=='uplink': changed['network1']['ports'][0]['_uuid']='reused-uplink'
                if kind=='gateway': changed['network1']['external_ids']['hostname']='wrong-gateway'
                if kind=='patch': changed['network1']['ports'][-1]['interfaces'][0]['options']['peer']='wrong-peer'
                w.cfg['hosts']=w.cfg['gateway_hosts']
                w.transport.host.side_effect=lambda host,args:json.dumps(changed[host])
                with self.assertRaises(RuntimeError): w.takeover()
                self.assertFalse(clock.sleeps)
                final=json.loads((root/'ns-ovn-takeover.json').read_text())
                self.assertEqual(final['status'],'FAIL'); self.assertFalse(final['retryable'])
                saved=json.loads((root/'ns-host-attempt-0001.json').read_text())
                self.assertEqual(saved['network1'],changed['network1'])

    def test_repeated_host_collection_timeouts_cannot_extend_deadline(self):
        with tempfile.TemporaryDirectory() as d,self.clock() as clock:
            root=pathlib.Path(d); w,hosts=self.fixture(root)
            w.hosts=Mock(side_effect=subprocess.TimeoutExpired('mock-host-read',.1))
            with self.assertRaisesRegex(RuntimeError,'timed out'): w.takeover()
            self.assertAlmostEqual(clock.now,.5); self.assertEqual(w.hosts.call_count,3)
            w.transport.run.assert_not_called()
            self.assertEqual(json.loads((root/'ns-ovn-takeover.json').read_text())['status'],'FAIL')

    def test_conflicting_ovn_gateway_identity_is_not_retried(self):
        for kind in ('chassis','gateway-mac'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as d,self.clock() as clock:
                root=pathlib.Path(d); w,hosts=self.fixture(root); w.hosts=Mock(return_value=hosts)
                db=json.loads(w.transport.run.return_value)
                if kind=='chassis': db['Port_Binding'][0]['chassis']='wrong-chassis'
                else: db['Logical_Router_Port'][0]['mac']='fa:16:3e:99:99:99'
                w.transport.run.return_value=json.dumps(db)
                with self.assertRaises(RuntimeError): w.takeover()
                self.assertEqual(w.hosts.call_count,1); self.assertFalse(clock.sleeps)
                self.assertEqual(json.loads((root/'ns-ovn-takeover.json').read_text())['status'],'FAIL')

    def test_failed_startup_cannot_leave_previously_successful_takeover_authoritative(self):
        with tempfile.TemporaryDirectory() as d,self.clock():
            root=pathlib.Path(d); w,hosts=self.fixture(root)
            (root/'runtime.json').write_text('{broken')
            with self.assertRaises(ValueError): w.takeover()
            self.assertNotEqual(json.loads((root/'ns-ovn-takeover.json').read_text())['status'],'PASS')


class IntegrationTests(unittest.TestCase):
    def fixture(self,root,ingress=False):
        ready_evidence(root); save(root/'pre-cleanup.json',dict(status='PASS')); save(root/'post-cleanup.json',dict(status='PASS')); cfg=config(ingress); c,nets,*_=cloud(cfg); before=scope.snapshot(c,cfg)
        nets[1].provider_network_type='geneve'; nets[1].mtu=1392; after=scope.snapshot(c,cfg,True)
        for k,t in [('control_plane_downtime.start',1),('db_migration.start',2),('db_migration.end',3),('phase08.end',4),('control_plane_downtime.end',5)]:
            (root/'metrics').mkdir(exist_ok=True); (root/'metrics'/k).write_text(str(t))
        save(root/'runtime.json',dict(ns_enabled=True,phase_marker_schema_version=2)); markers=restoration_markers(root)
        observers={}; baselines={}; profiles={}; fences={}
        for direction in ('egress','ingress'):
            if not cfg.get(direction): continue
            rc=raw_config(); rc.update(direction=direction,probe=cfg[direction]['probe'],boot='guest-boot' if direction=='egress' else cfg['ingress']['observer']['boot'],run=root.name,
                source_ip=cfg['egress']['guest']['ip'] if direction=='egress' else cfg['ingress']['observer']['ip'],
                expected_peer=before['gateway']['fixed_ips'][0]['ip_address'] if direction=='egress' else cfg['ingress']['observer']['ip'],lifetime=cfg['lifetime'])
            r=raw(rc); observers[direction]=dict(config=rc,start=anchor(r,2),end=anchor(r,12)); fences[direction]=8
            baselines[direction]=dict(boot='guest-boot')
            profiles['egress' if direction=='egress' else 'ingress_guest']=profile(cfg[direction]['guest'])
            save(root/('ns-'+direction+'-raw.json'),r)
        if ingress: profiles['ingress']=dict(product_uuid=cfg['ingress']['observer']['product_uuid'],boot=cfg['ingress']['observer']['boot'])
        save(root/'ns-config.json',cfg); save(root/'ns-before.json',before); save(root/'ns-after.json',after)
        save(root/'ns-lifecycle.json',dict(config_sha256=hashlib.sha256(json.dumps(cfg,sort_keys=True).encode()).hexdigest(),observers=observers))
        save(root/'ns-ovn-takeover.json',dict(status='PASS')); save(root/'ns-guest-baselines.json',baselines)
        save(root/'network-mtu-plan.json',dict(networks=[dict(network=uid(7),target_mtu=1392)]))
        save(root/'ns-post-identities.json',dict(status='PASS',controller_markers=markers,profiles=profiles,established_at=6))
        save(root/'ns-recovery.json',dict(status='PASS',controller_markers=markers,established_at=6,fences=fences))
        return cfg

    def test_complete_ns_pass_is_independent_of_application_and_pair_a_metrics(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); self.fixture(root)
            save(root/'ew-collection-failure.json',dict(status='FAIL')); save(root/'tenant-dataplane-probe.json',dict(status='UNAVAILABLE'))
            self.assertEqual(measurement.report(root)['status'],'PASS')

    def test_pending_report_preserves_open_failure_diagnostics_and_blocks_cleanup(self):
        import workload_validation
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); self.fixture(root)
            life=json.loads((root/'ns-lifecycle.json').read_text()); entry=life['observers']['egress']; del entry['end']
            save(root/'ns-lifecycle.json',life)
            save(root/'ns-egress-raw.json',raw(entry['config'],failures=(10,11,12)))
            recovery=json.loads((root/'ns-recovery.json').read_text()); recovery['status']='PENDING'; save(root/'ns-recovery.json',recovery)
            result=measurement.report(root); observed=result['directions']['egress']
            self.assertEqual(result['status'],'UNAVAILABLE'); self.assertEqual(observed['status'],'UNAVAILABLE')
            self.assertEqual(observed['observed_samples'],dict(attempts=10,successes=7,failures=3))
            self.assertEqual(observed['sampled_outage_windows'][-1]['failures'],3)
            self.assertIsNone(observed['sampled_outage_windows'][-1]['recovery_monotonic'])
            self.assertIsNone(observed['sampled_outage_windows'][-1]['duration_seconds'])
            self.assertIsNone(observed['longest_recovered_outage_seconds']); self.assertIsNone(observed['attempts'])
            self.assertTrue(observed['diagnostics_only']); self.assertIn('completed recovery',observed['acceptance_reason'])
            self.assertFalse(workload_validation.validation_ready(root))
            # Even a stale successful report cannot authorize stopping observers.
            save(root/'migration-report.json',dict(result='SUCCESS'))
            w=object.__new__(workflow.Workflow); w.root=root; w.life=life; w.call=Mock()
            with self.assertRaises(RuntimeError): w.finalize()
            w.call.assert_not_called()
            subprocess.run([sys.executable,str(ROOT/'scripts/migration_report.py'),str(root),'run','fixture'],capture_output=True,text=True,check=True)
            final=json.loads((root/'migration-report.json').read_text())
            self.assertEqual(final['result'],'MIGRATED_VALIDATION_INCOMPLETE')
            self.assertEqual(final['north_south']['directions']['egress']['observed_samples']['failures'],3)

    def test_both_directions_pass_and_one_corrupt_direction_never_erases_the_other(self):
        for bad in ('missing','invalid-json','wrong-identity'):
            with self.subTest(bad=bad),tempfile.TemporaryDirectory() as d:
                root=pathlib.Path(d); self.fixture(root,ingress=True)
                original=measurement.report(root); self.assertEqual(original['status'],'PASS')
                path=root/'ns-ingress-raw.json'
                if bad=='missing': path.unlink()
                if bad=='invalid-json': path.write_text('{broken')
                if bad=='wrong-identity':
                    evidence=json.loads(path.read_text()); evidence['rows'][4]['boot']='different'; save(path,evidence)
                result=measurement.report(root)
                self.assertEqual(result['status'],'UNAVAILABLE')
                self.assertEqual(result['directions']['ingress']['status'],'UNAVAILABLE')
                self.assertIn('reason',result['directions']['ingress'])
                self.assertEqual(result['directions']['egress'],original['directions']['egress'])

    def test_incomplete_acceptance_keeps_observations_without_authoritative_success(self):
        for missing in ('ns-ovn-takeover.json','metrics/control_plane_downtime.end','ns-post-identities.json','ns-recovery.json'):
            with self.subTest(missing=missing),tempfile.TemporaryDirectory() as d:
                root=pathlib.Path(d); self.fixture(root); (root/missing).unlink()
                result=measurement.report(root); observed=result['directions']['egress']
                self.assertEqual(result['status'],'UNAVAILABLE'); self.assertEqual(observed['status'],'UNAVAILABLE')
                self.assertEqual(observed['observed_samples']['failures'],2)
                self.assertEqual(observed['sampled_outage_windows'][0]['failures'],2)
                self.assertIsNone(observed['longest_recovered_outage_seconds'])
                self.assertTrue(observed['diagnostics_only'])

    def test_failed_or_corrupt_recovery_and_missing_direction_lifecycle_keep_diagnostics(self):
        for bad in ('failed','invalid-json','malformed-fences','missing-ingress-lifecycle'):
            with self.subTest(bad=bad),tempfile.TemporaryDirectory() as d:
                root=pathlib.Path(d); self.fixture(root,ingress=True)
                if bad=='missing-ingress-lifecycle':
                    life=json.loads((root/'ns-lifecycle.json').read_text()); del life['observers']['ingress']; save(root/'ns-lifecycle.json',life)
                elif bad=='invalid-json': (root/'ns-recovery.json').write_text('{broken')
                else:
                    recovery=json.loads((root/'ns-recovery.json').read_text())
                    if bad=='failed': recovery['status']='FAIL'
                    else: recovery['fences']=[]
                    save(root/'ns-recovery.json',recovery)
                result=measurement.report(root)
                self.assertEqual(result['status'],'UNAVAILABLE')
                self.assertEqual(result['directions']['egress']['observed_samples']['failures'],2)
                self.assertEqual(result['directions']['egress']['sampled_outage_windows'][0]['failures'],2)

    def test_complete_report_remains_stable_when_collection_appends_post_end_gap(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); self.fixture(root)
            original=measurement.report(root); path=root/'ns-egress-raw.json'; evidence=json.loads(path.read_text())
            extra=raw(evidence['state']['config'],count=13)['rows'][-1]
            for key in ('mono','http_end','end_mono'): extra[key]+=100
            evidence['rows'].append(extra); evidence['state'].update(seq=13,last_mono=extra['end_mono']); save(path,evidence)
            result=measurement.report(root); self.assertEqual(result['status'],'PASS')
            direction=result['directions']['egress']; outside=direction.pop('outside_window_anomalies')
            self.assertTrue(outside); expected=original['directions']['egress']; expected.pop('outside_window_anomalies')
            self.assertEqual(direction,expected)

    def test_corrupt_guest_baseline_does_not_hide_independent_external_observer_diagnostics(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); self.fixture(root,ingress=True)
            (root/'ns-guest-baselines.json').write_text('{broken')
            result=measurement.report(root)
            self.assertEqual(result['status'],'UNAVAILABLE')
            self.assertEqual(result['directions']['ingress']['observed_samples']['failures'],2)
            self.assertTrue(result['directions']['ingress']['diagnostics_only'])
            self.assertIn('source identity',result['directions']['ingress']['acceptance_reason'])

    def test_source_only_stale_identity_or_wrong_target_cannot_pass(self):
        for kind in ('source-only','stale-markers','wrong-boot','changed-fip','wrong-mtu','pre-anchor'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as d:
                root=pathlib.Path(d); self.fixture(root)
                if kind=='source-only': (root/'metrics/control_plane_downtime.end').unlink()
                if kind=='stale-markers': (root/'metrics/control_plane_downtime.end').write_text('9')
                if kind=='wrong-boot':
                    value=json.loads((root/'ns-post-identities.json').read_text()); value['profiles']['egress']['boot']='replacement'; save(root/'ns-post-identities.json',value)
                if kind=='changed-fip':
                    value=json.loads((root/'ns-after.json').read_text()); value['fips']=[{'id':'foreign'}]; save(root/'ns-after.json',value)
                if kind=='wrong-mtu':
                    value=json.loads((root/'ns-after.json').read_text()); value['guest_networks']['egress']['mtu']=1400; save(root/'ns-after.json',value)
                if kind=='pre-anchor':
                    value=json.loads((root/'ns-recovery.json').read_text()); value['fences']['egress']=11; save(root/'ns-recovery.json',value)
                self.assertEqual(measurement.report(root)['status'],'UNAVAILABLE')

    def test_report_acceptance_and_cleanup_gates_require_ns_without_erasing_pair_a(self):
        import workload_validation
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); self.fixture(root)
            self.assertTrue(workload_validation.validation_ready(root))
            cmd=[sys.executable,str(ROOT/'scripts/migration_report.py'),str(root),'run','fixture']
            result=subprocess.run(cmd,capture_output=True,text=True); self.assertEqual(result.returncode,0,result.stderr)
            report=json.loads((root/'migration-report.json').read_text()); self.assertEqual(report['result'],'SUCCESS'); self.assertEqual(report['north_south']['status'],'PASS')
            (root/'ns-egress-raw.json').unlink(); self.assertFalse(workload_validation.validation_ready(root))
            subprocess.run(cmd,capture_output=True,text=True,check=True)
            report=json.loads((root/'migration-report.json').read_text()); self.assertEqual(report['result'],'MIGRATED_VALIDATION_INCOMPLETE'); self.assertEqual(report['dataplane_probe']['status'],'PASS')

    def test_missing_or_conflicting_observer_path_and_stability_cannot_pass(self):
        for key,value in [('expected_peer',None),('source_ip','192.0.2.99'),('stable_samples',1),('run','foreign-run')]:
            with self.subTest(key=key),tempfile.TemporaryDirectory() as d:
                root=pathlib.Path(d); self.fixture(root)
                life=json.loads((root/'ns-lifecycle.json').read_text()); rc=life['observers']['egress']['config']
                if value is None: del rc[key]
                else: rc[key]=value
                save(root/'ns-lifecycle.json',life)
                evidence=json.loads((root/'ns-egress-raw.json').read_text()); evidence['state']['config']=rc
                save(root/'ns-egress-raw.json',evidence)
                self.assertEqual(measurement.report(root)['status'],'UNAVAILABLE')

    def test_application_failure_does_not_erase_complete_ns_report(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); self.fixture(root)
            save(root/'ew-measurement-config.json',dict(enabled=True,tcp_experiment_enabled=False))
            subprocess.run([sys.executable,str(ROOT/'scripts/migration_report.py'),str(root),'run','fixture'],capture_output=True,text=True,check=True)
            result=json.loads((root/'migration-report.json').read_text())
            self.assertEqual(result['result'],'MIGRATED_VALIDATION_INCOMPLETE')
            self.assertEqual(result['north_south']['status'],'PASS')
            self.assertEqual(result['dataplane_probe']['status'],'PASS')

    def test_orchestration_order_static_targets_and_no_reset_hook(self):
        def tasks(name): return [t for p in yaml.safe_load((ROOT/'playbooks'/name).read_text()) for t in p.get('tasks',[])]
        phase4=tasks('04-validation-workloads.yml'); self.assertEqual(phase4[-2]['vars']['ns_action'],'start')
        phase7=tasks('07-migrate-db.yml'); freeze=next(i for i,t in enumerate(phase7) if '--freeze-start' in t.get('ansible.builtin.command',{}).get('argv',[]))
        ready=next(i for i,t in enumerate(phase7) if t.get('vars',{}).get('ns_action')=='ready'); self.assertLess(ready,freeze)
        self.assertEqual(phase7[freeze-1]['ansible.builtin.command']['argv'][2],'ready')
        ns_gate=phase7[ready]
        self.assertNotIn('validation_workloads_enabled',str(ns_gate)); self.assertNotIn('failed_when',ns_gate)
        helper=yaml.safe_load((ROOT/'playbooks/ns-operation-tasks.yml').read_text())[0]
        self.assertIn('ns_failure_is_evidence',helper['failed_when'])
        phase8=tasks('08-cutover.yml'); retire=next(i for i,t in enumerate(phase8) if t.get('vars',{}).get('ns_action')=='retire')
        deploy=next(i for i,t in enumerate(phase8) if t['name']=='Deploy OVN controller'); self.assertLess(retire,deploy)
        self.assertGreater(next(i for i,t in enumerate(phase8) if t.get('vars',{}).get('ns_action')=='takeover'),deploy)
        phase12=tasks('12-workload-validation.yml'); recovery=next(i for i,t in enumerate(phase12) if t.get('vars',{}).get('ns_action')=='recovery')
        self.assertLess(recovery,next(i for i,t in enumerate(phase12) if t['name'].startswith('Observe bounded EW')))
        self.assertNotIn('reset_action',(ROOT/'scripts/ns_workflow.py').read_text())
        self.assertNotIn('reboot',(ROOT/'scripts/ns_workflow.py').read_text())

    def test_resume_cannot_override_recorded_mode_or_external_cleanup_scope(self):
        from ansible.parsing.dataloader import DataLoader
        from ansible.playbook.conditional import Conditional
        from ansible.template import Templar
        tasks=yaml.safe_load((ROOT/'playbooks/resume-bootstrap.yml').read_text())[0]['tasks']
        gate=next(t['ansible.builtin.assert']['that'] for t in tasks if t['name'].startswith('Refuse N-S mode overrides'))
        for recorded,effective,version,accepted in [(False,False,None,True),(True,True,1,True),
                (True,False,1,False),(False,True,None,False),(True,True,99,False)]:
            runtime=dict(ns_enabled=recorded)
            if version is not None: runtime['ns_schema_version']=version
            values=dict(ns_enabled=effective,resume_runtime=dict(content=base64.b64encode(json.dumps(runtime).encode()).decode()))
            loader=DataLoader(); condition=Conditional(loader=loader); condition.when=gate
            with self.subTest(recorded=recorded,effective=effective,version=version):
                self.assertEqual(condition.evaluate_conditional(Templar(loader=loader,variables=values),values),accepted)

    def test_fixture_gateway_roles_resolve_without_runtime_facts(self):
        from ansible.inventory.manager import InventoryManager
        from ansible.parsing.dataloader import DataLoader
        from ansible.playbook.conditional import Conditional
        from ansible.template import Templar
        loader=DataLoader(); inventory=InventoryManager(loader=loader,sources=[str(ROOT/'tests/fixtures/reset-inventory.ini'),str(ROOT/'tests/fixtures/ns-inventory.ini')])
        groups={name:[h.name for h in group.get_hosts()] for name,group in inventory.groups.items()}
        task=yaml.safe_load((ROOT/'playbooks/ns-config-tasks.yml').read_text())[0]
        condition=Conditional(loader=loader); condition.when=task['ansible.builtin.assert']['that']
        values=dict(groups=groups)
        self.assertTrue(condition.evaluate_conditional(Templar(loader=loader,variables=values),values))
        groups['ovn-controller-network'].append('compute1')
        self.assertFalse(condition.evaluate_conditional(Templar(loader=loader,variables=values),values))


if __name__=='__main__': unittest.main()
