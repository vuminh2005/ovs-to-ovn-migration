"""Exercise unchanged traffic semantics with different host counts and names."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import uuid

import yaml

import host_network
import ovn_ready
import router_host
from tests.support import ROOT
from tests.validation import test_four_pairs as placement_fixtures
from validation_placement import resolve as placement


class HostNetworkTests(unittest.TestCase):
    def inspect(self, index, gateway=False, target=False, bad_ip=False):
        ip = f'192.0.2.{index+1}'
        identity = str(uuid.UUID(int=index+1))
        cfg = dict(tunnel_interface='overlay'+str(index), external=gateway,
            external_interfaces=['uplink'+str(index)], physnet='edge')
        def command(*args):
            self.assertEqual(args[:6], ('ip', '-j', '-4', 'addr', 'show', 'dev'))
            self.assertEqual(args[-1], cfg['tunnel_interface'])
            return json.dumps([dict(mtu=9000, addr_info=[dict(family='inet', local=ip, prefixlen=24)])])
        def ovs(*args):
            if args[0] == 'port-to-br':
                self.assertEqual(args[1], cfg['external_interfaces'][0])
                return 'external-br'
            self.assertEqual(args, ('--format=json', '--columns=external_ids',
                                    'list', 'Open_vSwitch', '.'))
            return json.dumps(dict(headings=['external_ids'], data=[[
                ['map', [['system-id', identity], ['ovn-encap-ip', ip],
                         ['ovn-bridge-mappings', 'edge:external-br']]]]]))
        def get(section, key):
            return {'local_ip':'203.0.113.9' if bad_ip else ip,
                    'bridge_mappings':'edge:external-br'}[key]
        with patch.object(host_network, 'command', side_effect=command), \
             patch.object(host_network, 'ovs', side_effect=ovs), \
             patch.object(host_network.configparser.ConfigParser, 'read', return_value=['source.ini']), \
             patch.object(host_network.configparser.ConfigParser, 'get', side_effect=get):
            return host_network.inspect(cfg, target)

    def test_four_and_twenty_node_networks_use_their_own_interfaces_and_ips(self):
        for gateways, computes in ((1, 2), (3, 16), (2, 5)):
            rows = [self.inspect(i, i < gateways) for i in range(gateways+computes)]
            self.assertEqual(len({r['system_id'] for r in rows}), len(rows))
            for i, row in enumerate(rows):
                self.assertEqual(row['tunnel_interface'], 'overlay'+str(i))
                self.assertEqual(row['tunnel_mtu'], 9000)
                if i < gateways:
                    self.assertEqual(row['external_bridge'], 'external-br')

    def test_source_interface_must_own_the_effective_ovs_tunnel_ip(self):
        with self.assertRaisesRegex(RuntimeError, 'does not own'):
            self.inspect(0, bad_ip=True)

    def test_target_uses_ovn_encapsulation_and_exact_physnet_mapping(self):
        self.assertEqual(self.inspect(2, gateway=True, target=True)['status'], 'PASS')


class ReadinessTests(unittest.TestCase):
    def topology(self, count):
        hosts, chassis, encaps = {}, [], []
        for i in range(count):
            host, encap = 'arbitrary-'+str(i), 'encap-'+str(i)
            wanted = dict(system_id='system-'+str(i), tunnel_ip='192.0.2.'+str(i+1))
            hosts[host] = wanted
            chassis.append(dict(name=wanted['system_id'], hostname=host, encaps=[encap]))
            encaps.append(dict(_uuid=encap, type='geneve', ip=wanted['tunnel_ip']))
        return hosts, chassis, encaps

    def test_identity_checks_cover_arbitrary_counts_and_ignore_unrelated_stale_chassis(self):
        for count in (3, 7, 19, 41):
            hosts, chassis, encaps = self.topology(count)
            self.assertEqual(ovn_ready.chassis_missing(hosts, chassis+[dict(name='stale')], encaps), [])
            chassis[-1]['name'] = 'wrong-host-with-same-count'
            self.assertIn('arbitrary-'+str(count-1), ovn_ready.chassis_missing(hosts, chassis, encaps)[0])

    def test_wrong_tunnel_ip_and_duplicate_chassis_do_not_pass(self):
        hosts, chassis, encaps = self.topology(3)
        encaps[-1]['ip'] = '203.0.113.1'
        self.assertEqual(len(ovn_ready.chassis_missing(hosts, chassis, encaps)), 1)
        chassis.append(copy.deepcopy(chassis[0]))
        self.assertEqual(len(ovn_ready.chassis_missing(hosts, chassis, encaps)), 2)

    def test_original_ports_use_one_batch_and_preserve_chassis_up_requirements(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)/'ports'; ports = ['port-'+str(i) for i in range(2000)]
            p.write_text('\n'.join(ports)+'\n')
            cfg = dict(ports_path=str(p), ports_sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
                       sb='tcp:test', timeout=1, poll=.01)
            rows = [dict(logical_port=port, chassis='chassis', up=True) for port in ports]
            with patch.object(ovn_ready, 'query', return_value=rows) as query:
                self.assertEqual(ovn_ready.wait(cfg, 'bindings')['checked'], 2000)
                query.assert_called_once()
            rows[0]['up'] = [True]
            self.assertEqual(ovn_ready.ports_missing(ports, rows), [])
            rows[1]['chassis'] = []
            rows[2]['up'] = False
            self.assertEqual(ovn_ready.ports_missing(ports, rows), ports[1:3])
            p.write_text('foreign-port\n')
            with patch.object(ovn_ready, 'query') as query:
                with self.assertRaisesRegex(RuntimeError, 'hash changed'):
                    ovn_ready.wait(cfg, 'bindings')
                query.assert_not_called()


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'metrics').mkdir()
        self.hosts = ['edge-a', 'edge-b', 'edge-c']
        self.inventory = self.root/'inventory'
        self.inventory.write_text('[network]\n'+'\n'.join(h+' ansible_connection=local' for h in self.hosts)+'\n')
        (self.root/'inventory.before').write_bytes(self.inventory.read_bytes())
        self.v = NS(root=self.root, cfg=dict(topology_discovery=True, network_hosts=self.hosts,
            run='same-run', inventory=str(self.inventory), timeout=60))
        self.first, self.second = str(uuid.uuid4()), str(uuid.uuid4())
        self.locations = {self.first:'edge-b', self.second:'edge-c'}

    def fake_ansible(self, args, **kwargs):
        extra = json.loads(Path(args[-1][1:]).read_text())
        for host in self.hosts:
            namespaces = ['qrouter-'+r+' (id: 1)' for r,h in self.locations.items() if h == host]
            (Path(extra['router_host_observations'])/('host-'+host+'.json')).write_text(
                json.dumps(dict(host=host, namespaces=namespaces)))
        return NS(returncode=0, stdout='', stderr='')

    def test_each_router_resolves_independently_and_arm_reuses_checkpoint(self):
        with patch.object(router_host.subprocess, 'run', side_effect=self.fake_ansible) as run:
            self.assertEqual(router_host.resolve(self.v, self.first, 'CHECK'), 'edge-b')
            self.assertEqual(router_host.resolve(self.v, self.second, 'CHECK'), 'edge-c')
            self.assertEqual(run.call_count, 2)
        (self.root/'metrics/control_plane_downtime.start').write_text('123')
        with patch.object(router_host.subprocess, 'run') as run:
            self.assertEqual(router_host.resolve(self.v, self.first, 'ARM'), 'edge-b')
            self.assertEqual(router_host.resolve(self.v, self.second, 'ARM'), 'edge-c')
            run.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, 'after freeze'):
                router_host.resolve(self.v, self.first, 'CHECK')

    def test_new_four_node_profile_resolves_both_routers_to_its_one_network(self):
        self.hosts = ['network1']
        self.v.cfg['network_hosts'] = self.hosts
        self.inventory.write_text('[network]\nnetwork1 ansible_connection=local\n')
        (self.root/'inventory.before').write_bytes(self.inventory.read_bytes())
        self.locations = {self.first:'network1', self.second:'network1'}
        with patch.object(router_host.subprocess, 'run', side_effect=self.fake_ansible):
            self.assertEqual(router_host.resolve(self.v, self.first, 'CHECK'), 'network1')
            self.assertEqual(router_host.resolve(self.v, self.second, 'CHECK'), 'network1')

    def test_ambiguous_missing_or_partially_observed_router_fails(self):
        ns = 'qrouter-'+self.first
        for observations in ({'edge-a':[ns], 'edge-b':[ns], 'edge-c':[]},
                             {'edge-a':[], 'edge-b':[], 'edge-c':[]}, {'edge-a':[ns]}):
            with self.assertRaises(RuntimeError):
                router_host.select(self.first, self.hosts, observations)

    def test_router_move_and_inventory_change_cannot_retarget_checkpoint(self):
        with patch.object(router_host.subprocess, 'run', side_effect=self.fake_ansible):
            router_host.resolve(self.v, self.first, 'CHECK')
            self.locations[self.first] = 'edge-a'
            with self.assertRaisesRegex(RuntimeError, 'moved'):
                router_host.resolve(self.v, self.first, 'CHECK')
        self.inventory.write_text('[network]\nforeign\n')
        with self.assertRaisesRegex(RuntimeError, 'Inventory changed'):
            router_host.resolve(self.v, self.first, 'CHECK')

    def test_legacy_four_node_checkpoint_keeps_original_host(self):
        self.v.cfg = dict(pair_d_network_host='network1')
        with patch.object(router_host.subprocess, 'run') as run:
            self.assertEqual(router_host.resolve(self.v, 'legacy-router', 'ARM'), 'network1')
            run.assert_not_called()

    def test_real_ansible_transport_inspects_all_three_hosts(self):
        directory = self.root/'bin'; directory.mkdir()
        ip = directory/'ip'
        ip.write_text('#!'+sys.executable+'\nimport json,os\n'
            'locations=json.loads(os.environ["ROUTER_LOCATIONS"])\n'
            'print("\\n".join("qrouter-"+r+" (id: 1)" for r,h in locations.items() '
            'if h==os.environ["ROUTER_HOST"]))\n')
        ip.chmod(0o700)
        source = yaml.safe_load((ROOT/'playbooks/transport/router-host-tasks.yml').read_text())
        source[0]['become'] = False
        source[0]['environment'] = dict(ROUTER_HOST='{{ inventory_hostname }}')
        play = self.root/'transport.yml'; play.write_text(yaml.safe_dump(source))
        env = dict(os.environ, PATH=str(directory)+os.pathsep+os.environ['PATH'],
                   ROUTER_LOCATIONS=json.dumps(self.locations))
        with patch.object(router_host, 'playbook_path', return_value=play), patch.dict(os.environ, env):
            self.assertEqual(router_host.resolve(self.v, self.first, 'CHECK'), 'edge-b')
            self.assertEqual(router_host.resolve(self.v, self.second, 'CHECK'), 'edge-c')


class AutoPlacementTests(unittest.TestCase):
    def test_renamed_hosts_are_selected_without_fixed_compute_names(self):
        helper = placement_fixtures.PlacementTests()
        request = helper.request()
        request.update(pair_hosts=[], compute_hosts=['rack-b', 'rack-a'],
                       identities={'rack-a':['node1'], 'rack-b':['node2']})
        cloud = helper.cloud()
        result = placement(cloud, request)
        self.assertEqual([result[str(i)]['inventory_host'] for i in (0,1)], ['rack-a','rack-b'])
        cloud.compute.services.assert_called_once()
        cloud.network.agents.assert_called_once()

    def test_insufficient_ready_computes_cannot_claim_cross_compute_validation(self):
        helper = placement_fixtures.PlacementTests(); cloud = helper.cloud(); request = helper.request()
        request['pair_hosts'] = []
        cloud.compute.services.return_value[0].status = 'disabled'
        with self.assertRaisesRegex(RuntimeError, 'at least two ready'):
            placement(cloud, request)


class AnsibleHostTopologyTests(unittest.TestCase):
    def execute(self, network_count, compute_count, lab=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root/'bin'; binary.mkdir()
            run = root/'run'; run.mkdir()
            (root/'scripts').symlink_to(ROOT/'scripts', target_is_directory=True)
            plays_dir = root/'playbooks'; plays_dir.mkdir()
            ip = binary/'ip'
            ip.write_text('#!'+sys.executable+'\nimport json,os\n'
                'print(json.dumps([dict(mtu=int(os.environ["SIM_MTU"]), '
                'addr_info=[dict(family="inet",local=os.environ["SIM_IP"],prefixlen=24)])]))\n')
            docker = binary/'docker'
            docker.write_text('#!'+sys.executable+'\nimport json,os,sys,uuid\n'
                'if "port-to-br" in sys.argv: print("br-ex")\n'
                'elif sys.argv[-1]=="external_ids:system-id": '
                'print("chassis"+os.environ["SIM_INDEX"])\n'
                'elif sys.argv[-5:]==["--format=json", "--columns=external_ids", "list", "Open_vSwitch", "."]: '
                'print(json.dumps(dict(headings=["external_ids"], data=[[["map", '
                '[["system-id", "chassis"+os.environ["SIM_INDEX"]]],]]])))\n'
                'else: raise SystemExit(8)\n')
            for p in (ip, docker):
                p.chmod(0o700)
            inventory = root/'inventory.yml'
            networks, computes = {}, {}
            for i in range(network_count+compute_count):
                gateway = i < network_count
                name = ('network'+str(i+1) if gateway else 'compute'+str(i-network_count+1)) if lab else 'rack-host-'+str(i)
                iface = ('br-underlay' if gateway else 'eth1') if lab else ('overlay-bond' if gateway else 'tenant-link')
                address = f'10.1.0.{90 if lab and gateway else i+10}'
                config = root/(name+'.ini')
                config.write_text('[ovs]\nlocal_ip='+address+'\nbridge_mappings=physnet1:br-ex\n')
                row = dict(ansible_connection='local', ansible_python_interpreter=sys.executable,
                    sim_index=i+1, sim_ip=address, sim_tunnel=iface,
                    migration_source_ovs_config=str(config))
                (networks if gateway else computes)[name] = row
            inventory.write_text(yaml.safe_dump(dict(all=dict(children=dict(
                control=dict(hosts=dict(controller=dict(ansible_connection='local'))),
                network=dict(hosts=networks), compute=dict(hosts=computes))))))
            globals_file = root/'globals.yml'
            globals_file.write_text('tunnel_interface: "{{ sim_tunnel }}"\n'
                'neutron_external_interface: "{{ \'ns-ex\' if inventory_hostname in groups[\'network\'] else \'unused\' }}"\n')
            production = yaml.safe_load((ROOT/'playbooks/02-precheck.yml').read_text())
            source = copy.deepcopy(next(p for p in production if p['name']=='Phase 02 - Check source hosts and OVS networking'))
            keep = ('[02] Resolve effective tunnel interface per host',
                    '[02] Inspect effective networking and persistent chassis identity',
                    '[02] Publish effective per-host topology',
                    '[02] Check transport MTU for source VXLAN traffic')
            source.update(gather_facts=False, become=False,
                vars_files=[str(ROOT/'group_vars/all.yml'), str(ROOT/'group_vars/ns-lab.yml')],
                environment=dict(SIM_INDEX='{{ sim_index | default(0) }}', SIM_IP='{{ sim_ip | default("") }}',
                                 SIM_MTU='1450' if lab else '9000'))
            source['tasks'] = [t for t in source['tasks'] if t['name'] in keep]
            assembly = copy.deepcopy(next(p for p in production if p['name']=='Phase 02 - Resolve workload compute placement'))
            assembly.update(become=False, vars_files=source['vars_files'])
            assembly['tasks'] = assembly['tasks'][:3]
            prepare = dict(hosts='localhost', gather_facts=False, tasks=[{'ansible.builtin.set_fact':dict(
                migration_run_dir=str(run), chassis_hosts_runtime=list(networks)+list(computes))}])
            entry = plays_dir/'check.yml'; entry.write_text(yaml.safe_dump([prepare, source, assembly], sort_keys=False))
            env = dict(os.environ, PATH=str(binary)+os.pathsep+os.environ['PATH'], KOLLA_GLOBALS=str(globals_file))
            completed = subprocess.run(['ansible-playbook', '-i', str(inventory), str(entry)],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=90)
            self.assertEqual(completed.returncode, 0, completed.stdout+completed.stderr)
            saved = json.loads((run/'host-topology.json').read_text())
            self.assertEqual(set(saved), set(networks)|set(computes))
            for host in networks:
                self.assertEqual(saved[host]['external_interfaces'], ['ns-ex'])
                self.assertEqual(saved[host]['external_bridge'], 'br-ex')
            if lab:
                self.assertEqual(saved['network1']['tunnel_interface'], 'br-underlay')
                self.assertEqual(saved['network1']['tunnel_ip'], '10.1.0.90')
                self.assertEqual(saved['compute1']['tunnel_interface'], 'eth1')
                self.assertEqual(saved['compute2']['tunnel_mtu'], 1450)

    def test_actual_ansible_source_tasks_preserve_four_node_lab_profile(self):
        self.execute(1, 2, lab=True)

    def test_actual_ansible_source_tasks_accept_twenty_nodes_and_other_interfaces(self):
        self.execute(3, 16)
