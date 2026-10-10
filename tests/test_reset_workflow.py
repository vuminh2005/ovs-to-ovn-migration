"""Offline Work Item 2 boundaries; never run Ansible, SSH, Kolla or cloud APIs."""
import base64
import copy
import hashlib
import json
import pathlib
import subprocess
import sys
import tarfile
import tempfile
import unittest
import uuid
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import reset_workflow as reset
import reset_host_preflight as host
import reset_source_ready as source
import ew_provision


class RetentionTests(unittest.TestCase):
    def test_host_product_uuid_is_canonical_and_missing_zero_ff_are_refused(self):
        valid='B162D212-73BC-4427-81AE-702F70CF7D30'
        def read(path):
            return valid if str(path).endswith('product_uuid') else 'shared-machine-id'
        with patch.object(pathlib.Path,'read_text',read):
            self.assertEqual(host.identity()['product_uuid'],valid.lower())
        for invalid in ('not-a-uuid','00000000-0000-0000-0000-000000000000','ffffffff-ffff-ffff-ffff-ffffffffffff'):
            with self.subTest(invalid=invalid),patch.object(pathlib.Path,'read_text',return_value=invalid),self.assertRaises(RuntimeError):
                host.identity()
        with patch.object(pathlib.Path,'read_text',side_effect=FileNotFoundError),self.assertRaisesRegex(RuntimeError,'Missing/malformed'):
            host.identity()

    def test_symlink_alias_and_reverse_parent_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory); deleted = root/'volume'; deleted.mkdir()
            artifact = deleted/'private'; artifact.write_text('retained')
            alias = root/'alias'; alias.symlink_to(deleted, target_is_directory=True)
            mounts = [('8:1', '/', '/')]
            for path in (artifact, alias/'private', root):
                with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, 'overlaps'):
                    host.check_retention([host.path_evidence(path, mounts)], [host.path_evidence(deleted, mounts)])

    def test_bind_mount_alias_is_not_only_a_textual_prefix(self):
        mounts = host.mount_table('1 0 8:1 / / rw - ext4 /dev/x rw\n2 1 8:1 /data /alias rw - ext4 /dev/x rw')
        retained = host.path_evidence('/alias/key', mounts, False)
        deleted = host.path_evidence('/data', mounts, False)
        with self.assertRaises(RuntimeError): host.check_retention([retained], [deleted])
        host.check_retention([host.path_evidence('/private/key', mounts, False)], [deleted])

    def test_missing_required_artifact_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'Missing retained'):
            host.path_evidence('/nonexistent-ew-required-input', [('8:1','/','/')])

    def test_link_itself_in_deleted_tree_is_protected_even_if_target_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory); deleted=root/'config'; deleted.mkdir()
            target=root/'retained-image'; target.write_bytes(b'safe target')
            link=deleted/'image.qcow2'; link.symlink_to(target)
            mounts=[('8:1','/','/')]
            with self.assertRaisesRegex(RuntimeError,'overlaps'):
                host.check_retention([host.path_evidence(link,mounts)],[host.path_evidence(deleted,mounts)])

    def test_mount_escapes_are_decoded(self):
        self.assertEqual(host.mount_table(r'1 0 8:1 /data\040a /mnt\040b rw - ext4 /dev/x rw'), [('8:1','/data a','/mnt b')])


class HostDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=pathlib.Path(self.temp.name); self.volume=self.root/'volume'; self.volume.mkdir()
        self.retained=self.root/'retained'; self.retained.mkdir()
        self.rows=[dict(Name='/nova_compute', Config={'Labels':{'kolla_version':'18.8.0'}},
                        Mounts=[dict(Type='volume',Name='nova_compute',Source=str(self.volume)),
                                dict(Type='bind',Source='/var/log/journal',Destination='/var/log/journal',RW=False)])]
        self.volumes=[dict(Name='nova_compute',Driver='local',Options=None,Mountpoint=str(self.volume))]
        self.cfg=dict(required=[str(self.retained)],optional=[],delete_paths=[str(self.root/'custom-config')],
            inventory=str(self.root/'inventory'),cleanup=dict(reset.DATA_VOLUMES),interfaces=['eth0','eth1'],
            chassis=True,tunnel_interface='eth1',underlay_mtu=1450,remove_vips=[])
        self.namespace='qrouter-'+str(uuid.uuid4())
        def command(argv):
            if argv[:3]==['ip','netns','list']: return self.namespace
            if argv[:3]==['ip','-j','address']:
                return json.dumps([dict(ifname=n,mtu=1450,addr_info=[dict(family='inet',local='10.0.1.2')]) for n in ('eth0','eth1')])
            if argv[:3]==['docker','ps','-aq']: return 'container-id'
            if argv[:2]==['docker','inspect']: return json.dumps(self.rows)
            if argv[:4]==['docker','volume','ls','-q']: return 'nova_compute'
            if argv[:3]==['docker','volume','inspect']: return json.dumps(self.volumes)
            raise AssertionError('Unexpected command '+repr(argv))
        self.commands=patch.object(host,'command',side_effect=command); self.commands.start(); self.addCleanup(self.commands.stop)
        for context in (patch.object(host.os,'geteuid',return_value=0),patch.object(host.shutil,'which',return_value='/usr/bin/tool'),
                        patch.object(host.platform,'freedesktop_os_release',return_value={'ID':'ubuntu','VERSION_ID':'24.04'}),
                        patch.object(host,'identity',return_value={'product_uuid':str(uuid.uuid4())})):
            context.start(); self.addCleanup(context.stop)
        original=pathlib.Path.glob
        context=patch.object(pathlib.Path,'glob',lambda path,pattern:[] if str(path)=='/etc/kolla' else original(path,pattern))
        context.start(); self.addCleanup(context.stop)

    def test_actual_mount_records_retained_without_mutation(self):
        with patch.object(host.subprocess,'run') as mutation:
            result=host.discover(self.cfg)
        mutation.assert_not_called()
        self.assertEqual(result['status'],'PASS')
        self.assertIn(str(self.volume),[r['path'] for r in result['deletions']])
        self.assertEqual(result['containers'][0]['mounts'][1]['RW'],False)
        self.assertNotIn('/var/log/journal',[r['path'] for r in result['deletions']])

    def test_retained_file_within_actual_volume_blocks_destruction(self):
        path=self.volume/'image.qcow2'; path.write_bytes(b'protected')
        self.cfg['required'].append(str(path))
        with self.assertRaisesRegex(RuntimeError,'overlaps'): host.discover(self.cfg)

    def test_custom_storage_override_checked_before_destruction(self):
        self.cfg['cleanup']['nova_instance_datadir_volume']=str(self.retained)
        with self.assertRaisesRegex(RuntimeError,'overlaps'): host.discover(self.cfg)

    def test_nested_retained_symlink_into_volume_is_rejected(self):
        target=self.volume/'evidence'; target.write_text('preserve')
        (self.retained/'alias').symlink_to(target)
        with self.assertRaisesRegex(RuntimeError,'overlaps'): host.discover(self.cfg)

    def test_missing_tool_ambiguous_volume_or_foreign_namespace_blocks(self):
        with patch.object(host.shutil,'which',return_value=None),self.assertRaisesRegex(RuntimeError,'Missing reset prerequisite'):
            host.discover(self.cfg)
        self.volumes[0]['Options']={'device':'/somewhere'}
        with self.assertRaisesRegex(RuntimeError,'volume'): host.discover(self.cfg)
        self.volumes[0]['Options']=None; self.namespace='unrelated'
        with self.assertRaisesRegex(RuntimeError,'namespace'): host.discover(self.cfg)

    def test_unmounted_volume_fails_pre_destructive_preflight(self):
        self.rows[0]['Mounts']=[r for r in self.rows[0]['Mounts'] if r['Type'] != 'volume']
        with self.assertRaisesRegex(RuntimeError,'Unmounted Docker volumes'): host.discover(self.cfg)
        self.cfg['allow_orphan_volumes']=True  # only after durable destroy completion
        self.assertEqual(host.discover(self.cfg)['status'],'PASS')

    def test_uninspectable_running_qemu_blocks_guest_stop(self):
        self.cfg['compute']=True
        with patch.object(host,'qemu_processes',return_value=['123']),self.assertRaisesRegex(RuntimeError,'QEMU is running'):
            host.discover(self.cfg)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = pathlib.Path(self.temp.name)
        self.defaults = yaml.safe_load((ROOT/'group_vars/all.yml').read_text())
        self.spec = dict(generation='review-001', generation_root=str(self.base/'generations'),
            hosts=['controller','network1','network2','compute1','compute2'], control=['controller'],
            network=['network1','network2'], compute=['compute1','compute2'],
            inventory=str(self.base/'inventory'), invocation_inventory=str(self.base/'inventory'),
            kolla_venv=str(self.base/'venv'), globals_path=str(self.base/'kolla/globals.yml'),
            passwords=str(self.base/'kolla/passwords.yml'), config_path=str(self.base/'kolla/config'),
            source_globals=str(self.base/'retained/globals.yml'), source_config=str(self.base/'retained/config'),
            openrc=str(self.base/'kolla/admin-openrc.sh'), access_file=str(self.base/'private/access.yml'),
            credentials_file=str(self.base/'private/inputs.yml'), guest_key=str(self.base/'private/key'),
            host_known_hosts=str(self.base/'private/management-trust'), host_keys=[str(self.base/'private/host-key')],
            old_guest_known_hosts=str(self.base/'private/old-guests'), old_state=str(self.base/'old-state'),
            backup_root=str(self.base/'backup'), evidence_root=str(self.base/'backup'), snapshot_root=str(self.base/'snapshots'),
            capture_root=str(self.base/'captures'), delete_backups=False, underlay_mtu=1450, required_paths=[], host_protected={},
            topology=self.defaults['ew_workload_config'], provision_spec=copy.deepcopy(self.defaults['ew_provision_spec']), forward={})
        self.source_globals = dict(openstack_release='2024.1', kolla_base_distro='ubuntu', kolla_base_distro_version='noble',
            neutron_plugin_agent='openvswitch', neutron_tenant_network_types='vxlan', enable_openvswitch='yes', enable_ovn='no',
            enable_neutron_dvr='no', enable_neutron_agent_ha='no', enable_neutron_provider_networks='no',
            network_interface='eth0', api_interface='eth0', tunnel_interface='eth1', neutron_external_interface='neutron-ext',
            kolla_internal_vip_address='10.0.0.213', kolla_external_vip_address='10.0.0.213', nova_compute_virt_type='qemu')
        def file(path, value='retained'):
            p = pathlib.Path(path); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(value); p.chmod(0o600); return p
        self.file = file
        for key in ('inventory','globals_path','passwords','access_file','credentials_file','guest_key','host_known_hosts','old_guest_known_hosts'):
            file(self.spec[key])
        file(self.spec['openrc'])
        file(self.spec['host_keys'][0])
        file(self.spec['source_globals'], yaml.safe_dump(self.source_globals))
        cfg = pathlib.Path(self.spec['source_config'])
        file(cfg/'neutron.conf', '[DEFAULT]\nglobal_physnet_mtu=1450\n')
        file(cfg/'neutron/ml2_conf.ini', '[ml2]\npath_mtu=1450\noverlay_ip_version=4\n')
        file(cfg/'neutron/openvswitch_agent.ini', '[securitygroup]\nfirewall_driver=openvswitch\n')
        kolla = pathlib.Path(self.spec['kolla_venv'])/'share/kolla-ansible'
        file(kolla/'ansible/group_vars/all.yml','{}')
        file(kolla/'ansible/roles/neutron/templates/ml2_conf.ini.j2', '[ml2_type_geneve]\nmax_header_size = 38\n')
        script = file(kolla/'tools/cleanup-host', 'reviewed fake script')
        self.pins = patch.object(reset, 'DESTROY_PINS', {'tools/cleanup-host':reset.digest(script)}); self.pins.start(); self.addCleanup(self.pins.stop)
        image = file(self.base/'retained/image.qcow2')
        self.spec['provision_spec']['image'].update(path=str(image), sha256_file=str(file(self.base/'retained/image.sha256')))
        self.spec['provision_spec']['keypair_public_key'] = str(file(self.base/'private/key.pub','ssh-ed25519 verified-key'))
        self.spec['provision_spec']['secrets'] = {k:str(file(self.base/'private'/k, 'a'*48)) for k in ('db','mq')}
        self.spec['provision_spec']['bootstrap'] = {k:dict(path=str(file(self.base/'retained'/(k+'.py'))), sha256='unused mocked validation') for k in ('ew-db','ew-queue')}
        reset.save(pathlib.Path(self.spec['old_state'])/'resources.json', dict(resources={'server:old':str(uuid.uuid4())},
                   guests={'old':dict(boot='old')}, preserved_tasks={'old':{'task_id':'old'}}, application_deployment_pending={'keep':True}))
        self.identities = {h:dict(product_uuid=str(uuid.uuid5(uuid.NAMESPACE_DNS,h)), machine_id='shared-cloned-machine-id',hostname=h) for h in self.spec['hosts']}
        self.inventory = {'_meta':{'hostvars':{h:dict(reset.DATA_VOLUMES, kolla_internal_vip_address='10.0.0.213',
                                  kolla_external_vip_address='10.0.0.213') for h in self.spec['hosts']}}}
        self.checks = patch.object(reset.subprocess, 'check_output', side_effect=lambda argv,**kw:
            json.dumps(self.inventory) if '--list' in argv else 'ssh-ed25519 verified-key' if '-y' in argv else 'kolla-ansible 18.8.0\n'); self.checks.start(); self.addCleanup(self.checks.stop)
        self.image_validation = patch.object(reset, 'validated_spec', return_value=10*1024**3); self.image_validation.start(); self.addCleanup(self.image_validation.stop)
        self.tools=patch.object(reset.shutil,'which',return_value='/existing/tool'); self.tools.start(); self.addCleanup(self.tools.stop)
        self.sdk=patch.object(reset.importlib.util,'find_spec',return_value=object()); self.sdk.start(); self.addCleanup(self.sdk.stop)
        self.local_id = patch.object(reset, 'identity', return_value=self.identities['controller']); self.local_id.start(); self.addCleanup(self.local_id.stop)
        self.w = reset.Workflow(self.spec); self.events=[]; self.fail_stage=None; self.fail_once=True
        self.w.play = Mock(side_effect=self.play)

    def play(self, playbook, stage, variables):
        self.events.append(stage)
        if stage == self.fail_stage and self.fail_once:
            self.fail_once=False; raise RuntimeError('mocked '+stage+' failure')
        if stage == 'preflight':
            for h,cfg in variables['reset_probe'].items():
                reset.save(pathlib.Path(variables['reset_output'])/h, dict(status='PASS', identity=self.identities[h], hostname=h,
                    boot='original-'+h, underlay=dict(mtu=1450,ipv4=['10.0.1.1']), protected=cfg['required'], deletions=cfg['delete_paths']))
        elif stage == 'boundary':
            for h in self.spec['hosts']:
                reset.save(pathlib.Path(variables['reset_output'])/h, dict(identity=self.identities[h], boot='rebooted-'+h,
                    containers=[],volumes=[],namespaces=[],qemu=[],running_guests=[],generated_config=[],links=['eth0','eth1','neutron-ext']))
        elif playbook == 'ew-provision.yml':
            state = self.w.root/'provisioning/resources.json'
            if not state.exists():
                self.new = dict(resources={}, guests={}, preserved_tasks={})
                for vm in self.spec['topology']['servers']:
                    name=vm['name']; sid=str(uuid.uuid5(uuid.NAMESPACE_DNS,'new-'+name))
                    self.new['resources']['server:'+name]=sid
                    self.new['guests'][name]=dict(server=sid,port=str(uuid.uuid4()),ip=vm['ip'],boot='new-boot-'+name)
                for name in ('ew-client-a1','ew-client-a2','ew-client-b'): self.new['preserved_tasks'][name]=dict(task_id=str(uuid.uuid4()),status='done')
                reset.save(state,self.new)
            ready = dict(status='PASS',tasks={k:{'status':'PASS'} for k in self.new['preserved_tasks']},preserved_tasks={'status':'PASS'},
                         application_deployment=dict(changed_files=[],service_actions=[],environment_changed=False))
            reset.save(self.w.root/'provisioning/readiness.json',ready)
            reset.save(pathlib.Path(variables['ew_provision_result_file']),dict(status='PASS',action=variables['ew_provision_action'],changed=stage=='apply-first'))

    def complete(self):
        return self.w.execute('apply', True)

    def test_preflight_does_not_enter_destructive_stages_and_records_all_five(self):
        result=self.w.preflight()
        self.assertEqual(self.events,['preflight']); self.assertEqual(self.w.journal['stages'],{})
        self.assertEqual((result['source_mtu'],result['target_mtu']),(1400,1392))
        self.assertEqual(len(self.w.journal['host_identities']),5)
        self.assertEqual(self.w.path.stat().st_mode & 0o777,0o600)

    def test_controller_dispatch_writes_private_vars_and_uses_only_mocked_boundary(self):
        self.w.preflight()
        with patch.object(reset.subprocess,'run',return_value=NS(returncode=0)) as remote:
            reset.Workflow.play(self.w,'playbooks/reset-hosts.yml','offline-dispatch',{'proof':'only-paths'})
        args=remote.call_args.args[0]
        self.assertEqual(args[:3],[self.w.ansible,'-i',self.spec['inventory']])
        self.assertEqual(json.loads((self.w.root/'offline-dispatch-vars.json').read_text()),{'proof':'only-paths'})
        self.assertEqual((self.w.root/'offline-dispatch.log').stat().st_mode & 0o777,0o600)
        self.assertEqual(args[-2:],['-e','@'+str(self.w.root/'management-trust-vars.json')])

    def test_kolla_subprocesses_receive_the_same_strict_management_trust(self):
        self.w.preflight()
        trust=json.loads((self.w.root/'management-trust-vars.json').read_text())
        self.assertIn('StrictHostKeyChecking=yes',trust['reset_management_ssh_defaults']['ssh_args'])
        self.assertIn(self.spec['host_known_hosts'],trust['reset_management_ssh_defaults']['ssh_common_args'])
        destroy=json.loads((self.w.root/'destroy-vars.json').read_text())
        self.assertEqual(destroy['ansible_ssh_common_args'],trust['ansible_ssh_common_args'])
        # Play vars alone would not propagate into Kolla's child Ansible process.
        stages=yaml.safe_load((ROOT/'playbooks/reset-ovs-stages.yml').read_text())
        def tasks(rows):
            for task in rows:
                yield task
                yield from tasks(task.get('block',[]))
        commands=[t['ansible.builtin.shell'] for p in stages for t in tasks(p.get('tasks',[]))
                  if 'kolla-ansible --configdir' in t.get('ansible.builtin.shell','')]
        self.assertEqual(len(commands),4)
        for command in commands: self.assertIn('/management-trust-vars.json',command)

    def child_connections(self, argv):
        """Resolve real imported plays/extra-vars and build SSH argv, never run SSH."""
        from ansible import context
        from ansible.inventory.manager import InventoryManager
        from ansible.module_utils.common.collections import ImmutableDict
        from ansible.parsing.dataloader import DataLoader
        from ansible.playbook import Playbook
        from ansible.playbook.play_context import PlayContext
        from ansible.plugins.loader import connection_loader, init_plugin_loader
        from ansible.utils.collection_loader import AnsibleCollectionConfig
        from ansible.utils.vars import load_extra_vars, load_options_vars
        from ansible.template import Templar
        from ansible.vars.manager import VariableManager
        extra=[argv[i+1] for i,v in enumerate(argv) if v=='-e']
        if AnsibleCollectionConfig.collection_finder is None: init_plugin_loader()
        # Each child CLI starts a new process; isolate Ansible's process caches.
        with patch.object(context,'CLIARGS',ImmutableDict(extra_vars=extra)), \
                patch.object(load_extra_vars,'extra_vars',None,create=True), \
                patch.object(load_options_vars,'options_vars',None,create=True):
            loader=DataLoader()
            inv=InventoryManager(loader=loader,sources=[self.spec['inventory']])
            manager=VariableManager(loader=loader,inventory=inv)
            plays=Playbook.load(argv[3],variable_manager=manager,loader=loader)
            result=[]
            for play in plays.get_plays():
                if '02-precheck.yml' not in play.get_path() or play.hosts=='localhost': continue
                for h in inv.get_hosts(pattern=play.hosts):
                    values=manager.get_vars(play=play,host=h)
                    template=Templar(loader=loader,variables=values)
                    resolved={k:template.template(v) for k,v in values.items() if k.startswith('ansible_')}
                    connection=connection_loader.get('ssh',PlayContext())
                    connection.set_options(var_options=resolved)
                    cmd=[v.decode() for v in connection._build_command('ssh','ssh',h.name,'true')]
                    result.append((h.name,connection,cmd))
            return result

    def trust_fixture(self):
        self.inventory['_meta']['hostvars']['compute1'].update(
            ansible_host='10.0.2.101',
            ansible_ssh_common_args='-o ProxyJump=trusted-jump -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null',
            ansible_ssh_args='-o ServerAliveInterval=17 -o StrictHostKeyChecking=no',
            ansible_ssh_private_key_file=self.spec['host_keys'][0],ansible_port=2222,
            ansible_ssh_host_key_checking=False)
        self.inventory['_meta']['hostvars']['compute2']['ansible_ssh_private_key_file']=str(self.file(self.base/'private/compute2-key'))
        self.file(self.spec['inventory'],yaml.safe_dump({'all':{'children':{
            role:{'hosts':{h:self.inventory['_meta']['hostvars'][h] for h in self.spec[role]}}
            for role in ('control','network','compute')}}}))
        self.complete()  # only mocked cloud/SSH/service boundaries from setUp
        del self.w.play  # exercise actual Workflow.play and its child argv

    @staticmethod
    def first_ssh_option(argv, name):
        for i,arg in enumerate(argv):
            if arg=='-o' and argv[i+1].split('=',1)[0].lower()==name.lower():
                return argv[i+1].split('=',1)[1]

    def assert_connections_strict(self, rows):
        self.assertEqual({h for h,_,_ in rows},set(self.spec['hosts']))
        for h,connection,cmd in rows:
            self.assertTrue(connection.get_option('host_key_checking'))
            self.assertEqual(self.first_ssh_option(cmd,'StrictHostKeyChecking'),'yes')
            self.assertEqual(self.first_ssh_option(cmd,'UserKnownHostsFile'),'"'+self.spec['host_known_hosts']+'"')
            self.assertEqual(self.first_ssh_option(cmd,'ControlPath'),'none')
            self.assertEqual(self.first_ssh_option(cmd,'UpdateHostKeys'),'no')
            if h=='compute1':
                self.assertEqual(connection.get_option('private_key_file'),self.spec['host_keys'][0])
                self.assertEqual(connection.get_option('port'),2222)
                self.assertEqual(self.first_ssh_option(cmd,'ProxyJump'),'trusted-jump')
                self.assertEqual(self.first_ssh_option(cmd,'ServerAliveInterval'),'17')
            if h=='compute2':
                self.assertEqual(connection.get_option('private_key_file'),str(self.base/'private/compute2-key'))

    def test_source_ready_imports_resolve_reviewed_trust_and_per_host_options(self):
        self.trust_fixture()
        with patch.object(reset.subprocess,'run',return_value=NS(returncode=0)) as child:
            self.w.operation('source-ready')
        rows=self.child_connections(child.call_args.args[0])
        self.assert_connections_strict(rows)

    def test_first_verify_second_children_resolve_the_same_management_trust(self):
        self.trust_fixture()
        trust_before=pathlib.Path(self.spec['host_known_hosts']).read_bytes()
        guest_before=pathlib.Path(self.spec['old_guest_known_hosts']).read_bytes()
        for action,label in (('apply','apply-first'),('verify','verify'),('apply','apply-second')):
            with self.subTest(label=label),patch.object(reset.subprocess,'run',return_value=NS(returncode=0)) as child:
                self.w.provision(action,label)
                self.assert_connections_strict(self.child_connections(child.call_args.args[0]))
        self.assertEqual(pathlib.Path(self.spec['host_known_hosts']).read_bytes(),trust_before)
        self.assertEqual(pathlib.Path(self.spec['old_guest_known_hosts']).read_bytes(),guest_before)
        selected=json.loads((self.w.root/'generation-vars.yml').read_text())
        self.assertEqual(selected['ew_guest_known_hosts'],str(self.w.root/'guest-known-hosts'))

    def test_custom_trust_and_ambient_permissive_settings_cannot_weaken_child_ssh(self):
        self.spec['host_known_hosts']=str(self.file(self.base/'private/custom management trust'))
        ambient=dict(ANSIBLE_HOST_KEY_CHECKING='False',ANSIBLE_SSH_HOST_KEY_CHECKING='False',
            ANSIBLE_SSH_ARGS='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ServerAliveCountMax=8',
            ANSIBLE_SSH_COMMON_ARGS='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null',
            ANSIBLE_SSH_EXTRA_ARGS='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null')
        with patch.dict(reset.os.environ,ambient):
            self.trust_fixture()
            with patch.object(reset.subprocess,'run',return_value=NS(returncode=0)) as child:
                self.w.operation('source-ready')
            rows=self.child_connections(child.call_args.args[0])
        self.assert_connections_strict(rows)
        controller=next(cmd for h,_,cmd in rows if h=='controller')
        self.assertEqual(self.first_ssh_option(controller,'ServerAliveCountMax'),'8')

    def test_conflicting_management_key_failure_is_not_ignored_or_trust_replaced(self):
        self.trust_fixture()
        known=pathlib.Path(self.spec['host_known_hosts']); before=known.read_bytes()
        def refused(argv,**kwargs):
            self.assert_connections_strict(self.child_connections(argv))
            # SSH boundary is mocked: a changed host key produces failure.
            return NS(returncode=255)
        with patch.object(reset.subprocess,'run',side_effect=refused),self.assertRaisesRegex(RuntimeError,'source-ready failed'):
            self.w.operation('source-ready')
        self.assertEqual(known.read_bytes(),before)

    def test_missing_policy_cannot_launch_a_child_ansible_process(self):
        with patch.object(reset.subprocess,'run') as child,self.assertRaisesRegex(RuntimeError,'Missing reviewed management trust'):
            reset.Workflow.play(self.w,'ew-provision.yml','unreviewed',{})
        child.assert_not_called()

    def test_child_stage_extra_vars_cannot_override_management_policy(self):
        self.trust_fixture()
        path=self.w.root/'generation-vars.yml'; variables=json.loads(path.read_text())
        variables.update(ansible_ssh_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null',
                         ansible_ssh_common_args='-o UserKnownHostsFile=/dev/null',ansible_ssh_host_key_checking=False)
        reset.save(path,variables)
        with patch.object(reset.subprocess,'run',return_value=NS(returncode=0)) as child:
            self.w.provision('verify','verify')
        self.assert_connections_strict(self.child_connections(child.call_args.args[0]))

    def test_failed_preflight_intent_cannot_be_rebound(self):
        self.fail_stage='preflight'
        with self.assertRaises(RuntimeError): self.w.preflight()
        self.file(self.spec['access_file'],'different access input')
        with self.assertRaisesRegex(RuntimeError,'intent'): self.w.preflight()

    def test_shell_metacharacters_in_effective_deletion_override_fail_before_hosts(self):
        self.inventory['_meta']['hostvars']['compute1']['nova_instance_datadir_volume']='/data/a /private'
        with self.assertRaisesRegex(RuntimeError,'unsafe Kolla shell deletion'): self.complete()
        self.assertEqual(self.events,[])

    def test_protected_path_override_missing_stops_before_destroy(self):
        self.spec['required_paths']=[str(self.base/'missing')]
        with self.assertRaisesRegex(RuntimeError,'Missing retained'): self.complete()
        self.assertEqual(self.events,[])

    def test_source_inside_deleted_override_or_alias_refused(self):
        for alias in (False,True):
            with self.subTest(alias=alias):
                self.spec['config_path']=self.spec['source_config']
                if alias:
                    link=self.base/'alias'; link.symlink_to(self.spec['source_config']); self.spec['config_path']=str(link)
                with self.assertRaisesRegex(RuntimeError,'overlaps'): self.complete()
                self.assertEqual(self.events,[])

    def test_delete_evidence_override_refused(self):
        self.spec['delete_backups']=True
        with self.assertRaisesRegex(RuntimeError,'evidence is protected'): self.complete()
        self.assertEqual(self.events,[])

    def test_controller_preflight_failure_prevents_any_destructive_stage(self):
        self.fail_stage='preflight'
        with self.assertRaises(RuntimeError): self.complete()
        self.assertEqual(self.events,['preflight']); self.assertIsNone(self.w.journal)

    def test_missing_host_evidence_blocks_all_destruction(self):
        original=self.play
        def missing(play,stage,variables):
            original(play,stage,variables)
            if stage=='preflight': (self.w.root/'hosts/network2').unlink()
        self.w.play.side_effect=missing
        with self.assertRaises(FileNotFoundError): self.complete()
        self.assertEqual(self.events,['preflight'])

    def test_different_active_incomplete_generation_is_refused(self):
        base=pathlib.Path(self.spec['generation_root']); base.mkdir(mode=0o700)
        reset.save(base/'active.json',{'generation':'unfinished-other'})
        path=self.file(self.base/'invocation.json',json.dumps(self.spec))
        with patch.object(sys,'argv',['reset_workflow.py','preflight',str(path)]),self.assertRaisesRegex(RuntimeError,'Another reset'):
            reset.main()
        self.assertEqual(self.events,[])

    def test_inventory_key_alias_is_retained_and_bound(self):
        key=self.file(self.base/'private/inventory-key')
        self.inventory['_meta']['hostvars']['compute1']['ansible_private_key_file']=str(key)
        self.w.preflight()
        self.assertIn(str(key),self.w.journal['management_keys'])
        variables=self.w.play.call_args.args[2]
        self.assertIn(str(key),variables['reset_probe']['controller']['required'])
        key.write_text('changed-key')
        with self.assertRaisesRegex(RuntimeError,'management key binding'): self.w.preflight()

    def test_changed_input_generation_reuse_is_refused(self):
        self.w.preflight(); self.file(self.spec['credentials_file'],'different mapping')
        with self.assertRaisesRegex(RuntimeError,'Generation/input conflict'): self.complete()
        self.assertEqual(self.events,['preflight'])

    def test_duplicate_product_uuid_is_refused_despite_inventory_names(self):
        self.identities['compute2']=self.identities['compute1']
        with self.assertRaisesRegex(RuntimeError,'distinct'): self.complete()

    def test_unchanged_generation_preflight_can_repeat_without_new_state(self):
        self.w.preflight(); before=copy.deepcopy(self.w.journal); self.w.preflight()
        self.assertEqual(self.w.journal,before)
        vars=json.loads((self.w.root/'generation-vars.yml').read_text())
        self.assertEqual(vars['ew_provision_state_dir'],str(self.w.root/'provisioning'))
        self.assertEqual(vars['ew_guest_known_hosts'],str(self.w.root/'guest-known-hosts'))
        self.assertNotIn('ew_workloads_enabled',vars)
        self.assertFalse((self.w.root/'guest-known-hosts').exists())

    def test_new_reset_requires_confirmation_and_continue_cannot_start_it(self):
        for action in ('apply','continue'):
            with self.subTest(action=action),self.assertRaises(RuntimeError): self.w.execute(action,False)
        self.assertNotIn('stop-guests',self.events)

    def test_existing_state_lock_refuses_concurrent_work_without_rewriting(self):
        path=pathlib.Path(self.spec['old_state'])/'.lock'; path.write_text('lock bytes retained')
        with reset.locked(path),self.assertRaisesRegex(RuntimeError,'holds the state lock'):
            with reset.locked(path): self.fail('must not enter a second operation')
        self.assertEqual(path.read_text(),'lock bytes retained')

    def test_order_complete_acceptance_and_historical_archive(self):
        before=(pathlib.Path(self.spec['old_state'])/'resources.json').read_bytes()
        self.assertEqual(self.complete()['status'],'PASS')
        self.assertLess(self.events.index('source-ready'),self.events.index('apply-first'))
        self.assertEqual(self.events[-3:],['apply-first','verify','apply-second'])
        with tarfile.open(self.w.root/'historical/provisioning.tar') as stream:
            archived=stream.extractfile('old-state/resources.json').read()
        self.assertEqual(archived,before)
        self.assertEqual((pathlib.Path(self.spec['old_state'])/'resources.json').read_bytes(),before)
        self.assertNotIn('baseline',self.events)

    def test_resume_after_deploy_failure_never_destroys_again(self):
        self.fail_stage='deploy'
        with self.assertRaises(RuntimeError): self.complete()
        self.w.execute('continue',False)
        self.assertEqual(self.events.count('destroy'),1); self.assertEqual(self.events.count('stop-guests'),1)
        self.assertEqual(self.events.count('deploy'),2)
        with self.assertRaisesRegex(RuntimeError,'use continue'): self.w.execute('apply',True)

    def test_interrupted_destroy_never_replays_even_with_confirmation(self):
        self.fail_stage='destroy'
        with self.assertRaises(RuntimeError): self.complete()
        with self.assertRaisesRegex(RuntimeError,'Ambiguous destructive boundary'): self.w.execute('continue',True)
        self.assertEqual(self.events.count('destroy'),1)
        self.assertNotIn('deploy',self.events)

    def test_read_only_destroy_completion_proof_can_continue(self):
        self.fail_stage='destroy'
        with self.assertRaises(RuntimeError): self.complete()
        self.assertEqual(self.w.reconcile()['reconciled'],'destroy')
        self.w.execute('continue',False)
        self.assertEqual(self.events.count('destroy'),1)

    def test_partial_destroy_cannot_be_reconciled(self):
        self.fail_stage='destroy'
        with self.assertRaises(RuntimeError): self.complete()
        original=self.play
        def partial(play,stage,variables):
            original(play,stage,variables)
            if stage=='boundary':
                path=self.w.root/'boundary/compute1'; value=json.loads(path.read_text()); value['volumes']=['nova_compute']; reset.save(path,value)
        self.w.play.side_effect=partial
        with self.assertRaisesRegex(RuntimeError,'partial/ambiguous'): self.w.reconcile()
        self.assertNotIn('deploy',self.events)

    def test_reboot_completion_proof_prevents_duplicate_reboot(self):
        self.fail_stage='reboot-network1'
        with self.assertRaises(RuntimeError): self.complete()
        self.assertEqual(self.w.reconcile()['reconciled'],'reboot-network1')
        self.w.execute('continue',False)
        self.assertEqual(self.events.count('reboot-network1'),1)

    def test_reboot_proof_cannot_skip_clean_network_checks(self):
        self.fail_stage='reboot-network1'
        with self.assertRaises(RuntimeError): self.complete()
        original=self.play
        def dirty(play,stage,variables):
            original(play,stage,variables)
            if stage=='boundary':
                path=self.w.root/'boundary/network1'; row=json.loads(path.read_text()); row['links'].append('br-int'); reset.save(path,row)
        self.w.play.side_effect=dirty
        with self.assertRaisesRegex(RuntimeError,'Post-reboot clean-network'): self.w.reconcile()
        self.assertNotIn('deploy',self.events)

    def test_source_readiness_failure_stops_provisioning(self):
        self.fail_stage='source-ready'
        with self.assertRaises(RuntimeError): self.complete()
        self.assertNotIn('apply-first',self.events)
        self.assertEqual(self.w.journal['stages']['source-ready']['status'],'INTERRUPTED')
        self.w.execute('continue',False)
        self.assertEqual(self.events.count('destroy'),1)

    def test_fresh_provisioning_failure_retries_same_generation_without_destroy(self):
        self.fail_stage='apply-first'
        with self.assertRaises(RuntimeError): self.complete()
        self.w.execute('continue',False)
        self.assertEqual(self.events.count('apply-first'),2); self.assertEqual(self.events.count('destroy'),1)
        self.assertEqual(self.events.count('source-ready'),2)

    def test_changed_second_apply_is_not_accepted(self):
        original=self.play
        def changed(play,stage,variables):
            original(play,stage,variables)
            if stage=='apply-second': reset.save(self.w.root/'apply-second-result.json',dict(status='PASS',action='apply',changed=True))
        self.w.play.side_effect=changed
        with self.assertRaisesRegex(RuntimeError,'changed=false'): self.complete()
        self.assertFalse((self.w.root/'acceptance.json').exists())
        self.assertEqual(self.w.journal['stages']['apply-second']['status'],'INTERRUPTED')

    def test_keypair_name_can_be_preserved_but_resource_uuids_cannot(self):
        old=pathlib.Path(self.spec['old_state'])/'resources.json'
        previous=json.loads(old.read_text()); previous['resources']['keypair:ew-key']='ew-key'; reset.save(old,previous)
        original=self.play
        def with_keypair(play,stage,variables):
            original(play,stage,variables)
            if stage.startswith('apply'):
                path=self.w.root/'provisioning/resources.json'; state=json.loads(path.read_text()); state['resources']['keypair:ew-key']='ew-key'; reset.save(path,state)
        self.w.play.side_effect=with_keypair
        self.assertEqual(self.complete()['status'],'PASS')

    def test_old_server_uuid_cannot_be_reused_in_new_generation(self):
        old=pathlib.Path(self.spec['old_state'])/'resources.json'
        previous=json.loads(old.read_text()); previous['resources']['server:ew-app']=str(uuid.uuid5(uuid.NAMESPACE_DNS,'new-ew-app')); reset.save(old,previous)
        with self.assertRaisesRegex(RuntimeError,'old resource UUIDs'): self.complete()
        self.assertNotIn('verify',self.events)

    def test_changed_identity_receipt_or_service_action_cannot_pass(self):
        self.complete()
        first=json.loads((self.w.root/'first-state.json').read_text())
        ready=json.loads((self.w.root/'provisioning/readiness.json').read_text())
        result=json.loads((self.w.root/'apply-second-result.json').read_text())
        for key in ('resources','guests','preserved_tasks'):
            after=copy.deepcopy(first); after[key]['bad']='changed'
            with self.subTest(key=key),self.assertRaises(RuntimeError): reset.acceptance(first,after,ready,result)
        ready['application_deployment']['service_actions']=['restart']
        with self.assertRaises(RuntimeError): reset.acceptance(first,first,ready,result)

    def test_no_acceptance_before_all_reboot_and_final_checks(self):
        self.complete(); self.w.journal['stages']['reboot-compute2']['status']='STARTED'
        with self.assertRaisesRegex(RuntimeError,'incomplete'): self.w.accept()

    def test_source_configuration_rejects_ovn_or_implicit_mtu(self):
        self.source_globals['neutron_plugin_agent']='ovn'; self.file(self.spec['source_globals'],yaml.safe_dump(self.source_globals))
        with self.assertRaisesRegex(RuntimeError,'OVS source'): reset.source_configuration(self.spec)
        self.source_globals['neutron_plugin_agent']='openvswitch'; self.file(self.spec['source_globals'],yaml.safe_dump(self.source_globals))
        self.file(pathlib.Path(self.spec['source_config'])/'neutron.conf','[DEFAULT]\n')
        with self.assertRaisesRegex(RuntimeError,'Explicit source'): reset.source_configuration(self.spec)

    def test_reduced_tenant_mtu_in_retained_source_config_blocks_destroy(self):
        cfg=pathlib.Path(self.spec['source_config'])
        for stale in (1400,1392):
            with self.subTest(stale=stale):
                # Each candidate is a distinct invocation; an existing failed
                # preflight intent must still refuse changed generation inputs.
                self.spec['generation']='reduced-source-'+str(stale)
                self.w=reset.Workflow(self.spec); self.w.play=Mock(side_effect=self.play)
                self.file(cfg/'neutron.conf','[DEFAULT]\nglobal_physnet_mtu='+str(stale)+'\n')
                with self.assertRaisesRegex(RuntimeError,'reduced tenant-MTU source baseline'):
                    self.w.execute('apply',True)
                self.assertNotIn('stop-guests',self.events)
                self.assertNotIn('destroy',self.events)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.spec=dict(control=['controller'],network=['network1','network2'],compute=['compute1','compute2'])
        self.hosts={h:dict(hostname=h) for h in ['network1','network2','compute1','compute2']}
        from mtu_plan import calculate
        self.expected=calculate(dict(source_configs={'controller':dict(global_physnet_mtu=1450,path_mtu=1450,overlay_ip_version=4,mechanism_drivers='openvswitch,l2population',tenant_network_types='vxlan')},
            underlay={h:dict(mtu=1450,ipv4=['10.1.0.1']) for h in self.hosts},geneve_max_header_size=38))
        self.services=[NS(host=h,state='up',status='enabled') for h in self.spec['compute']]
        self.agents=[NS(host=h,agent_type=k,is_alive=True,is_admin_state_up=True) for h in self.hosts
            for k in ['Open vSwitch agent']+(['L3 agent','DHCP agent','Metadata agent'] if h in self.spec['network'] else [])]
        self.cloud=NS(compute=NS(services=lambda **kw:self.services,servers=lambda **kw:[]),
            network=NS(agents=lambda:self.agents,networks=lambda:[],routers=lambda:[],ips=lambda:[]))

    def test_empty_source_ready(self):
        self.assertEqual(source.check(self.cloud,self.spec,self.hosts,self.expected,self.expected,{})['status'],'PASS')

    def test_service_or_placement_failure_blocks(self):
        self.services[1].state='down'
        with self.assertRaisesRegex(RuntimeError,'placement'): source.check(self.cloud,self.spec,self.hosts,self.expected,self.expected,{})
        self.services[1].state='up'; self.agents[0].is_alive=False
        with self.assertRaisesRegex(RuntimeError,'agent'): source.check(self.cloud,self.spec,self.hosts,self.expected,self.expected,{})

    def test_reduced_mtu_or_uncheckpointed_cloud_cannot_become_source(self):
        observed=copy.deepcopy(self.expected); observed['validation_source_mtu']=1392
        with self.assertRaisesRegex(RuntimeError,'MTU'): source.check(self.cloud,self.spec,self.hosts,self.expected,observed,{})
        self.cloud.compute.servers=lambda **kw:[NS(id='unowned')]
        with self.assertRaisesRegex(RuntimeError,'uncheckpointed'): source.check(self.cloud,self.spec,self.hosts,self.expected,self.expected,{})


class TrustAndIntegrationTests(unittest.TestCase):
    def test_reused_ip_gets_verified_new_server_key_only_in_new_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory); old=root/'old-hosts'; old.write_text('192.168.101.11 old key\n')
            new=root/'new-hosts'
            key='ssh-ed25519 '+base64.b64encode(b'k'*64).decode()
            p=object.__new__(ew_provision.Provisioner); p.access_cfg={'guest_known_hosts':str(new)}
            p.state={'created':{'server:ew-app':'new-server'}}; p.timeout=5
            p.cloud=NS(compute=NS(get_server_console_output=Mock(return_value={'output':'-----BEGIN SSH HOST KEY KEYS-----\n'+key+'\n-----END SSH HOST KEY KEYS-----'})))
            with patch.object(ew_provision.subprocess,'run',return_value=NS(returncode=1)):
                p.trust_new_guest(dict(name='ew-app',server='new-server',ip='192.168.101.11'))
            self.assertEqual(old.read_text(),'192.168.101.11 old key\n'); self.assertIn(key,new.read_text())
            p.state={'created':{}}
            with patch.object(ew_provision.subprocess,'run',return_value=NS(returncode=1)),self.assertRaises(RuntimeError):
                p.trust_new_guest(dict(name='ew-app',server='foreign',ip='192.168.101.12'))

    def test_no_measurement_or_migration_import_and_static_hosts(self):
        entry=yaml.safe_load((ROOT/'reset-ew-lab.yml').read_text())
        self.assertEqual(len(entry),1); self.assertEqual(entry[0]['hosts'],'localhost')
        stages=yaml.safe_load((ROOT/'playbooks/reset-ovs-stages.yml').read_text())
        imports=[p['ansible.builtin.import_playbook'] for p in stages if 'ansible.builtin.import_playbook' in p]
        self.assertEqual(imports,['00-bootstrap.yml','02-precheck.yml'])
        for p in stages:
            if 'hosts' in p: self.assertNotIn('{{',p['hosts'])
        text=(ROOT/'scripts/reset_workflow.py').read_text()
        for forbidden in ('ew-baseline.yml','ew-migrate.yml','migrate-to-ovn.yml','ew-recover-credentials.yml'):
            self.assertNotIn(forbidden,text)


if __name__=='__main__': unittest.main()
