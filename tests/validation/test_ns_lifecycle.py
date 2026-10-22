import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from tests.support import ROOT
sys.path.insert(0,str(ROOT/'scripts'))
from extended_validation import Extended
from openstack.network.v2.network import Network
from openstack.network.v2.subnet import Subnet
from openstack.network.v2.port import Port
from openstack.network.v2.router import Router
from openstack.network.v2.floating_ip import FloatingIP
from openstack.network.v2.security_group import SecurityGroup
from openstack.network.v2.security_group_rule import SecurityGroupRule
from openstack.compute.v2.server import Server


class MemoryNetwork:
    def __init__(self):
        self.data={kind:{} for kind in ('network','subnet','port','router','security_group','ip')}
        self.rules=[]
        self.writes=[]
        self.classes=dict(network=Network,subnet=Subnet,port=Port,router=Router,security_group=SecurityGroup,ip=FloatingIP)

    def create(self,kind,**attrs):
        identifier=kind+'-'+str(len(self.data[kind])+1)
        if kind=='network': attrs.setdefault('provider_network_type','vxlan')
        if kind=='port':
            attrs.setdefault('fixed_ips',[dict(subnet_id='sub',ip_address='10.233.0.50')])
            attrs.update(status='ACTIVE',binding_host_id='host',binding_vif_type='ovs',device_id='',mac_address='fa:16:3e:01:02:03')
        if kind=='ip': attrs.update(status='ACTIVE',router_id=next(iter(self.data['router'])))
        resource=self.classes[kind](id=identifier,**attrs)
        self.data[kind][identifier]=resource
        self.writes.append(('create',kind,copy.deepcopy(attrs)))
        return resource

    def __getattr__(self,name):
        for kind in self.data:
            if name=='create_'+kind: return lambda _kind=kind,**attrs:self.create(_kind,**attrs)
            if name=='get_'+kind: return lambda identifier,_kind=kind:self.data[_kind][getattr(identifier,'id',identifier)]
            if name=='find_'+kind: return lambda identifier,_kind=kind:self.data[_kind].get(getattr(identifier,'id',identifier))
            if name=='delete_'+kind: return lambda identifier,ignore_missing=False,_kind=kind:self.data[_kind].pop(getattr(identifier,'id',identifier),None)
            collection={'security_group':'security_groups','ip':'ips'}.get(kind,kind+'s')
            if name==collection:
                return lambda _kind=kind,**query:[obj for obj in self.data[_kind].values() if all(getattr(obj,k,None)==v for k,v in query.items())]
        raise AttributeError(name)

    def security_group_rules(self,**kwargs): return self.rules

    def create_security_group_rule(self,**kwargs):
        rule=SecurityGroupRule(**kwargs);self.rules.append(rule);return rule

    def add_interface_to_router(self,router,subnet_id):
        sub=self.data['subnet'][subnet_id]
        port=self.create('port',network_id=sub.network_id,fixed_ips=[dict(subnet_id=subnet_id,ip_address='10.233.0.1')])
        port.device_id=router

    def update_router(self,router,**attrs):
        self.writes.append(('update','router',copy.deepcopy(attrs)))
        for key,value in attrs.items(): setattr(self.data['router'][router],key,value)
        gateway=attrs.get('external_gateway_info')
        if gateway and not any(p.device_id==router and p.network_id==gateway['network_id'] for p in self.data['port'].values()):
            port=self.create('port',network_id=gateway['network_id'],fixed_ips=gateway['external_fixed_ips'])
            port.device_id=router
        return self.data['router'][router]


class NSLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        placement={str(i):{'inventory_host':'compute'+str(i+1),'nova_host':'compute'+str(i+1),'hypervisor':'hv'+str(i+1),'neutron_host':'compute'+str(i+1)} for i in range(2)}
        self.cfg=dict(run='run',prefix='validation',ns=dict(enabled=True,endpoint_ip='10.1.0.187',port=18180,
            external_cidr='10.1.0.0/24',external_gateway='10.1.0.1',physnet='physnet1',mtu=1450,
            snat_ip='10.1.0.4',fip_ip='10.1.0.5',tenant_cidr='10.233.0.0/24',outbound_ip='10.233.0.11',inbound_ip='10.233.0.20'),
            timeout=2,lifetime=30,tcp_interval=.5,tcp_timeout=1,placement=placement,image='image',flavor='flavor')
        self.network=MemoryNetwork();self.compute=Mock();self.servers={}
        self.compute.servers.side_effect=lambda **query:[s for s in self.servers.values() if s.name==query.get('name')]
        self.compute.get_server.side_effect=lambda identifier:self.servers[identifier]
        self.v=NS(root=self.root,cfg=self.cfg,cloud=NS(network=self.network,compute=self.compute),
            wait_active=Mock(),wait_placement=Mock(),placement=Mock(return_value={'status':'PASS'}))
        self.x=Extended(self.v)
        def create(compute,target,**attrs):
            identifier='server-'+str(len(self.servers)+1)
            server=Server(id=identifier,name=attrs['name'],metadata=attrs['metadata'],status='ACTIVE')
            self.servers[identifier]=server
            self.network.data['port'][attrs['networks'][0]['port']].device_id=identifier
            return server
        self.creator=patch('validation_placement.create_on_target',side_effect=create).start()
        self.addCleanup(patch.stopall)
        def local(role,action,cfg):
            if role=='snat-server':
                (self.root/'ns/snat-server.jsonl').write_text('ready')
        patch.object(self.x,'local_process',side_effect=local).start()
        patch.object(self.x,'console_rows',return_value=[{'kind':'server'}]).start()

    def test_owned_flat_gateway_fip_and_two_computes_are_created_once(self):
        self.x.create_ns()
        original=copy.deepcopy(self.x.state['ns'])
        writes=len(self.network.writes)
        self.x.create_ns()
        self.assertEqual(len(self.network.writes),writes)
        self.assertEqual(self.x.state['ns'],original)
        self.assertEqual(self.creator.call_count,2)
        self.assertEqual(len(self.network.data['network']),2)
        self.assertEqual(len(self.network.data['subnet']),2)
        self.assertEqual(len(self.network.data['router']),1)
        self.assertEqual(len(self.network.data['ip']),1)
        s=self.x.state['ns']
        self.assertEqual(self.network.get_network(s['external_network']).provider_network_type,'flat')
        self.assertFalse(self.network.get_subnet(s['external_subnet']).is_dhcp_enabled)
        self.assertEqual(self.network.get_router(s['router']).external_gateway_info['external_fixed_ips'],
                         [{'subnet_id':s['external_subnet'],'ip_address':'10.1.0.4'}])
        self.assertEqual(s['vms']['0']['placement']['inventory_host'],'compute1')
        self.assertEqual(s['vms']['1']['placement']['inventory_host'],'compute2')
        inbound=self.network.get_ip(s['floating_ip'])
        self.assertEqual((inbound.floating_ip_address,inbound.fixed_ip_address),('10.1.0.5','10.233.0.20'))

    def test_lost_fip_reply_is_recovered_without_a_second_fip(self):
        original=self.network.create_ip
        def lost(**attrs):
            original(**attrs)
            raise RuntimeError('reply lost')
        with patch.object(self.network,'create_ip',side_effect=lost):
            with self.assertRaisesRegex(RuntimeError,'reply lost'): self.x.create_ns()
        self.x.create_ns()
        self.assertEqual(len(self.network.data['ip']),1)
        self.assertEqual(self.creator.call_count,2)

    def test_external_mtu_and_fip_mapping_changes_fail_preservation(self):
        self.x.create_ns()
        self.x.ns_snapshot()
        ext=self.network.get_network(self.x.state['ns']['external_network'])
        ext.mtu=1392
        with self.assertRaisesRegex(RuntimeError,'mapping changed'): self.x.ns_snapshot()
        ext.mtu=1450
        fip=self.network.get_ip(self.x.state['ns']['floating_ip'])
        fip.port_id='another-port'
        with self.assertRaisesRegex(RuntimeError,'mapping changed'): self.x.ns_snapshot()

    def test_renamed_or_replaced_guest_metadata_fails_audit(self):
        self.x.create_ns()
        vm=self.x.state['ns']['vms']['0']
        self.servers[vm['server']].metadata={'ovn_migration_run':'another-run'}
        with self.assertRaisesRegex(RuntimeError,'ownership changed'): self.x.ns_snapshot()

    def test_reserved_fip_owned_by_another_run_is_never_adopted(self):
        self.network.data['ip']['foreign']=FloatingIP(id='foreign',floating_ip_address='10.1.0.5',description='foreign',port_id='foreign-port')
        with self.assertRaisesRegex(RuntimeError,'another owner'): self.x.create_ns()
        self.assertEqual(len(self.network.data['ip']),1)
        self.creator.assert_not_called()


if __name__=='__main__':unittest.main()
