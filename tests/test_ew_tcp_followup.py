"""Offline two-port sockets, frozen-API activation and same-run MTU checkpoints."""
import copy
import json
import os
import pathlib
import socket
import tempfile
import threading
import time
import unittest
import subprocess
import sys
from unittest.mock import Mock, patch

import yaml
from test_ew_workload import fixture, raw_actor, runner, agent, ew, ROOT, v
import ew_tcp_experiment as tcp


def tcp_fixture(root):
    f=fixture(root); f.cfg.update(tcp_experiment_enabled=True,tcp_ports=[18080,18081])
    f.obj.cfg=f.cfg; f.obj.state['configuration']=copy.deepcopy(f.cfg)
    f.obj.session()['runners']={n:f.obj.runner_config(n,f.obj.session()) for n in ew.ACTORS}
    v.save(root/'ew-measurement-config.json',f.cfg); f.obj.commit()
    return f


class SocketTests(unittest.TestCase):
    def test_first_listener_then_explicit_second_bind_and_validated_echo(self):
        with tempfile.TemporaryDirectory() as d:
            ports=[]
            for _ in range(2):
                with socket.socket() as s: s.bind(('127.0.0.1',0)); ports.append(s.getsockname()[1])
            cfg=dict(output=d,run_id='tcp-local',boot='server-boot',ip='127.0.0.1',client_id='ew-app',
                tcp_experiment=dict(ports=ports),interval=.05)
            recorder=runner.Recorder(d); stop=threading.Event(); errors=[]
            def serve():
                try: runner.tcp_listeners(cfg,recorder,stop)
                except Exception as exc: errors.append(exc)
            thread=threading.Thread(target=serve); thread.start()
            try:
                deadline=time.monotonic()+2
                path=pathlib.Path(d)/'tcp-listeners.json'
                while not path.exists() and time.monotonic()<deadline: time.sleep(.01)
                self.assertIn(str(ports[0]),json.loads(path.read_text()))
                with self.assertRaises(OSError): runner.tcp_echo_attempt('127.0.0.1',ports[1],'tcp-local','server-boot')
                self.assertTrue(runner.tcp_echo_attempt('127.0.0.1',ports[0],'tcp-local','server-boot'))
                self.assertFalse(runner.tcp_echo_attempt('127.0.0.1',ports[0],'tcp-local','wrong-boot'))
                # Request alone never claims success; the listener records actual bind.
                runner.atomic_json(pathlib.Path(d)/'tcp-second.request.json',dict(run_id='tcp-local',boot='server-boot',port=ports[1]))
                while str(ports[1]) not in json.loads(path.read_text()) and time.monotonic()<deadline: time.sleep(.01)
                self.assertTrue(runner.tcp_echo_attempt('127.0.0.1',ports[1],'tcp-local','server-boot'))
            finally:
                stop.set(); thread.join(2); recorder.stream.close()
            self.assertFalse(thread.is_alive()); self.assertFalse(errors)
            rows=[json.loads(l) for l in (pathlib.Path(d)/'events.jsonl').read_text().splitlines()]
            self.assertEqual([r['port'] for r in rows if r['kind']=='tcp_listener_activated'],ports)
            with self.assertRaises(OSError): runner.tcp_echo_attempt('127.0.0.1',ports[0],'tcp-local','server-boot')

    def test_failed_bind_is_recorded_and_never_claimed_as_activation(self):
        with tempfile.TemporaryDirectory() as d,socket.socket() as occupied:
            occupied.bind(('127.0.0.1',0)); occupied.listen()
            port=occupied.getsockname()[1]; recorder=runner.Recorder(d)
            cfg=dict(output=d,run_id='owned',boot='boot',ip='127.0.0.1',tcp_experiment=dict(ports=[port,port+1]))
            with self.assertRaises(OSError): runner.tcp_listeners(cfg,recorder,threading.Event())
            recorder.stream.close()
            rows=[json.loads(l) for l in (pathlib.Path(d)/'events.jsonl').read_text().splitlines()]
            self.assertEqual(rows[0]['kind'],'tcp_listener_failed')
            self.assertFalse((pathlib.Path(d)/'tcp-listeners.json').exists())

    def test_framing_accepts_segmented_tcp_and_rejects_partial_echo(self):
        conn=Mock(); conn.recv.side_effect=[b'{"nonce":',b'"x"}\n']
        self.assertEqual(runner.tcp_receive(conn),dict(nonce='x'))
        conn.recv.side_effect=[b'{',b'']
        with self.assertRaises(ValueError): runner.tcp_receive(conn)


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=pathlib.Path(self.tmp.name); self.f=tcp_fixture(self.root); self.obj=self.f.obj
        (self.root/'metrics').mkdir(); (self.root/'metrics/control_plane_downtime.start').write_text('100')
        for net in self.f.networks.values(): net.mtu=1392
        for profile in self.f.profiles.values(): profile['interfaces'][0]['mtu']=1392
        self.obj.readiness('pre-freeze'); self.obj.commit()
        self.f.cloud.network.reset_mock(); self.f.cloud.compute.reset_mock(); self.f.tr.reset_mock()

    def statuses(self,second=True):
        session=self.obj.session(); bound=dict(port=18081,seq=9,run_id=session['run_id'],boot=session['guests']['ew-app']['boot'])
        client=dict(seq=14,mono=99,successes=5,success_sequences=[10,11,12,13,14],
            run_id=session['run_id'],boot=session['guests']['ew-client-b']['boot'])
        return {'ew-app':dict(status='RUNNING',last_event_seq=8,tcp_listeners={'18081':bound} if second else {}),
                'ew-client-b':dict(status='RUNNING',last_event_seq=8,current_mono=100,tcp_progress={'18081':client} if second else {})}

    def test_actual_bind_and_fresh_new_port_echo_checkpoint_without_any_cloud_call(self):
        activated=[False]
        def operation(vm,access,payload,timeout):
            name=payload['config']['client_id']
            if payload['action']=='tcp-activate':
                persisted=json.loads(self.obj.path.read_text())['sessions']['migration']['tcp_activation']
                self.assertEqual(persisted['status'],'REQUESTED'); activated[0]=True
                return dict(status='REQUESTED')
            return self.statuses(activated[0])[name]
        self.f.tr.operation.side_effect=operation
        with patch.object(ew.time,'time',return_value=150): result=self.obj.activate_tcp()
        self.assertEqual(result['status'],'PASS'); self.assertEqual(result['binding']['seq'],9)
        self.f.cloud.network.get_port.assert_not_called(); self.f.cloud.network.ports.assert_not_called()
        self.f.cloud.compute.get_server.assert_not_called()
        self.assertTrue(all(c.args[1]['namespace'].startswith('qrouter-') for c in self.f.tr.operation.call_args_list))

    def test_request_without_bind_or_echo_fails_and_preserves_intent(self):
        self.f.tr.operation.side_effect=lambda vm,access,payload,timeout: dict(status='REQUESTED') if payload['action']=='tcp-activate' else self.statuses(False)[payload['config']['client_id']]
        with patch.object(ew.time,'monotonic',side_effect=[0,0,0,0,100]):
            with self.assertRaises(TimeoutError): self.obj.activate_tcp()
        self.assertEqual(self.obj.session()['tcp_activation']['status'],'FAIL')
        self.assertEqual(self.obj.session()['tcp_activation']['client_fence'],8)

    def test_missing_freeze_or_late_activation_cannot_pass(self):
        marker=self.root/'metrics/control_plane_downtime.start'; marker.unlink()
        with self.assertRaisesRegex(RuntimeError,'after freeze'): self.obj.activate_tcp()
        marker.write_text('100'); (self.root/'metrics/db_migration.start').write_text('200')
        with self.assertRaisesRegex(RuntimeError,'before DB'): self.obj.activate_tcp()
        self.f.tr.operation.assert_not_called()

    def test_guest_activation_targets_only_running_exact_server_unit(self):
        cfg=self.obj.session()['runners']['ew-app']
        with patch.object(agent,'ROOT',self.root),patch.object(agent,'status',return_value=dict(status='RUNNING')):
            (self.root/cfg['run_id']).mkdir()
            previous=os.umask(0o077)
            try: self.assertEqual(agent.activate_tcp(cfg)['status'],'REQUESTED')
            finally: os.umask(previous)
            self.assertEqual((self.root/cfg['run_id']/'tcp-second.request.json').stat().st_mode & 0o777,0o644)
            before=(self.root/cfg['run_id']/'tcp-second.request.json').read_bytes()
            agent.activate_tcp(cfg)
            self.assertEqual((self.root/cfg['run_id']/'tcp-second.request.json').read_bytes(),before)
            with self.assertRaises(RuntimeError): agent.activate_tcp(self.obj.session()['runners']['ew-client-b'])

    def test_dedicated_ports_cannot_collide_with_configured_application_endpoint(self):
        self.obj.catalog['configuration']['endpoints']['api']['port']=18080
        with self.assertRaisesRegex(RuntimeError,'non-application ports'):
            self.obj.runner_config('ew-app',self.obj.session())


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=pathlib.Path(self.tmp.name); self.f=tcp_fixture(self.root); session=self.f.obj.session()
        streams={}
        for actor in ('ew-app','ew-client-b'):
            directory=self.f.obj.base/actor; raw_actor(directory,session['runners'][actor])
            rows=[json.loads(l) for l in (directory/'events.jsonl').read_text().splitlines()]
            boot=session['guests'][actor]['boot']; extra=[]
            if actor=='ew-app':
                extra=[dict(kind='tcp_listener_activated',port=p,mono=t) for p,t in ((18080,.05),(18081,2.5))]
            else:
                for port in (18080,18081):
                    for t in (.1,.2,.3,.4,.5,2,3,4,5,6,7,7.2,7.4):
                        extra.append(dict(kind='tcp_echo',port=port,mono=t,ok=port==18080 or t>=3))
            for r in extra: r.update(run_id=session['run_id'],boot=boot,utc=str(r['mono']))
            rows=sorted(rows+extra,key=lambda r:r['mono'])
            for i,r in enumerate(rows): r['seq']=i+1
            (directory/'events.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
            summary=json.loads((directory/'summary.json').read_text()); summary['last_event_seq']=len(rows); v.save(directory/'summary.json',summary)
            session['initial_coverage']=session.get('initial_coverage',{}); session['initial_coverage'][actor]=max(r['seq'] for r in rows if r['mono']<=.5)
            streams[actor]=rows
        bound=next(r for r in streams['ew-app'] if r['kind']=='tcp_listener_activated' and r['port']==18081)
        session['tcp_activation']=dict(status='PASS',server=self.f.catalog['servers']['ew-app']['server'],
            port=self.f.catalog['servers']['ew-app']['port'],verified_at_epoch=150,binding=bound,
            server_fence=max(r['seq'] for r in streams['ew-app'] if r['mono']<=2),
            client_fence=max(r['seq'] for r in streams['ew-client-b'] if r['mono']<=2))
        self.f.obj.commit(); (self.root/'metrics').mkdir()
        (self.root/'metrics/control_plane_downtime.start').write_text('100'); (self.root/'metrics/db_migration.start').write_text('200')
        for name,value in (('db_migration.end',230),('phase06.end',250),('control_plane_downtime.end',300)):
            (self.root/'metrics'/name).write_text(str(value))
        for net in self.f.networks.values(): net.mtu=1392; net.provider_network_type='geneve'
        for profile in self.f.profiles.values(): profile['interfaces'][0]['mtu']=1392
        self.f.tr.operation.return_value=dict(status='RUNNING',last_event_seq=session['tcp_activation']['client_fence'])
        with patch.object(ew.time,'time',return_value=350): self.f.obj.tcp_recovery_evidence()

    def test_two_ports_pass_with_real_bind_identity_and_new_port_echo_evidence(self):
        result=tcp.report(self.root,ew.metrics.assess)
        self.assertEqual(result['status'],'PASS',result)
        self.assertGreater(result['ports']['18081']['pre_activation_attempts'],0)
        self.assertEqual(result['ports']['18081']['failures'],0)

    def test_late_or_missing_activation_or_new_port_echoes_never_pass(self):
        session=self.f.obj.session(); session['tcp_activation']['verified_at_epoch']=250; self.f.obj.commit()
        self.assertEqual(tcp.report(self.root,ew.metrics.assess)['status'],'UNAVAILABLE')
        session['tcp_activation']['verified_at_epoch']=150; self.f.obj.commit()
        path=self.f.obj.base/'ew-client-b'/'events.jsonl'
        rows=[json.loads(l) for l in path.read_text().splitlines()]
        for r in rows:
            if r['kind']=='tcp_echo' and r['port']==18081: r['ok']=False
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        self.assertEqual(tcp.report(self.root,ew.metrics.assess)['status'],'FAIL')
        session.pop('tcp_activation'); self.f.obj.commit()
        self.assertEqual(tcp.report(self.root,ew.metrics.assess)['status'],'UNAVAILABLE')

    def test_application_reconciliation_failure_does_not_change_valid_tcp_result(self):
        path=self.f.obj.base/'ew-client-b'/'summary.json'; summary=json.loads(path.read_text())
        summary['server_stats']['processed']=2; v.save(path,summary)
        self.assertEqual(ew.metrics.assess(path.parent,'migration')['status'],'FAIL')
        self.assertEqual(tcp.report(self.root,ew.metrics.assess)['status'],'PASS')

    def test_freeze_only_source_only_or_missing_post_ovn_evidence_cannot_pass(self):
        session=self.f.obj.session(); original=copy.deepcopy(session['tcp_recovery'])
        for field in ('tcp_recovery',):
            session.pop(field); self.f.obj.commit()
            self.assertEqual(tcp.report(self.root,ew.metrics.assess)['status'],'UNAVAILABLE')
        session['tcp_recovery']=original; self.f.obj.commit()
        marker=self.root/'metrics/control_plane_downtime.end'; marker.unlink()
        self.assertEqual(tcp.report(self.root,ew.metrics.assess)['status'],'UNAVAILABLE')
        marker.write_text('300')
        session['tcp_recovery']['guests']['ew-app']['network_type']='vxlan'; self.f.obj.commit()
        self.assertEqual(tcp.report(self.root,ew.metrics.assess)['status'],'UNAVAILABLE')

    def test_controller_restoration_markers_require_complete_correct_order(self):
        for name in ('db_migration.end','phase06.end','control_plane_downtime.end'):
            path=self.root/'metrics'/name; original=path.read_text()
            for value in (None,'150','nan'):
                with self.subTest(marker=name,value=value):
                    if value is None: path.unlink(missing_ok=True)
                    else: path.write_text(value)
                    self.assertEqual(tcp.report(self.root,ew.metrics.assess)['status'],'UNAVAILABLE')
            path.write_text(original)

    def test_new_schema_uses_canonical_takeover_marker(self):
        v.save(self.root/'runtime.json',dict(phase_marker_schema_version=2))
        self.assertEqual(tcp.report(self.root,ew.metrics.assess)['status'],'UNAVAILABLE')
        (self.root/'metrics/phase08.end').write_text('250')
        session=self.f.obj.session()
        session['tcp_recovery']['controller_markers']=tcp.restoration_markers(self.root); self.f.obj.commit()
        self.assertEqual(tcp.report(self.root,ew.metrics.assess)['status'],'PASS')

    def test_checkpointed_target_guest_identity_boot_and_journal_are_required(self):
        session=self.f.obj.session(); original=copy.deepcopy(session['tcp_recovery'])
        for actor in ('ew-app','ew-client-b'):
            for key,value in (('server','other'),('port','other'),('fixed_ip','other'),('boot','other'),
                              ('network','other'),('network_mtu',1400)):
                with self.subTest(actor=actor,key=key):
                    session['tcp_recovery']=copy.deepcopy(original)
                    session['tcp_recovery']['guests'][actor][key]=value; self.f.obj.commit()
                    self.assertEqual(tcp.report(self.root,ew.metrics.assess)['status'],'UNAVAILABLE')
        session['tcp_recovery']=original; self.f.obj.commit()
        path=self.root/'network-mtu-plan.json'; journal=json.loads(path.read_text())
        journal['networks'][0]['target_mtu']=1400; v.save(path,journal)
        self.assertEqual(tcp.report(self.root,ew.metrics.assess)['status'],'UNAVAILABLE')

    def test_pre_anchor_successes_cannot_prove_post_ovn_recovery_on_either_port(self):
        session=self.f.obj.session(); original=session['tcp_recovery']['client_fence']
        rows=[json.loads(l) for l in (self.f.obj.base/'ew-client-b'/'events.jsonl').read_text().splitlines()]
        session['tcp_recovery']['client_fence']=max(r['seq'] for r in rows if r['mono']<=7)
        self.f.obj.commit()
        self.assertEqual(tcp.report(self.root,ew.metrics.assess)['status'],'UNAVAILABLE')
        session['tcp_recovery']['client_fence']=original; self.f.obj.commit()
        for port in (18080,18081):
            changed=copy.deepcopy(rows)
            for row in changed:
                if row['kind']=='tcp_echo' and row['port']==port and row['seq']>original: row['ok']=False
            path=self.f.obj.base/'ew-client-b'/'events.jsonl'
            path.write_text(''.join(json.dumps(r)+'\n' for r in changed))
            self.assertEqual(tcp.report(self.root,ew.metrics.assess)['status'],'FAIL')

    def test_phase12_recovery_persists_tcp_target_before_application_failure(self):
        session=self.f.obj.session(); session.pop('tcp_recovery'); self.f.obj.commit()
        self.f.tr.operation.side_effect=lambda vm,access,payload,timeout: (
            dict(e2e=False,dependencies=False) if payload['action']=='check' else
            dict(status='RUNNING',last_event_seq=session['tcp_activation']['client_fence']))
        with patch.object(ew.time,'time',return_value=350):
            with self.assertRaisesRegex(RuntimeError,'end-to-end'): self.f.obj.recover()
        persisted=json.loads(self.f.obj.path.read_text())['sessions']['migration']
        self.assertEqual(persisted['tcp_recovery']['status'],'PASS')
        self.assertEqual(persisted['readiness']['recovery']['status'],'FAIL')
        self.assertEqual(tcp.report(self.root,ew.metrics.assess)['status'],'PASS')
        plays=yaml.safe_load((ROOT/'playbooks/12-workload-validation.yml').read_text())
        self.assertTrue(any('ew_workload.py' in task.get('ansible.builtin.shell','') and
                            ' recovery ' in task['ansible.builtin.shell'] for play in plays for task in play['tasks']))

    def test_recovery_anchor_survives_retry_without_resetting_sequence_fence(self):
        original=copy.deepcopy(self.f.obj.session()['tcp_recovery'])
        self.f.tr.operation.reset_mock()
        self.f.obj.tcp_recovery_evidence()
        self.assertEqual(self.f.obj.session()['tcp_recovery'],original)
        self.f.tr.operation.assert_not_called()

    def test_recovered_tcp_failures_are_counted_with_client_monotonic_window(self):
        path=self.f.obj.base/'ew-client-b'/'events.jsonl'; rows=[json.loads(l) for l in path.read_text().splitlines()]
        for r in rows:
            if r['kind']=='tcp_echo' and r['port']==18080 and r['mono'] in (3,4): r['ok']=False
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        result=tcp.report(self.root,ew.metrics.assess)
        self.assertEqual(result['status'],'PASS'); self.assertEqual(result['ports']['18080']['failures'],2)
        self.assertEqual(result['ports']['18080']['failure_windows'][0]['observed_seconds'],2)
        self.assertNotIn('packet_loss_percent',result)

    def test_disabled_and_historical_experiment_are_not_tested(self):
        self.f.cfg.pop('tcp_experiment_enabled'); v.save(self.root/'ew-measurement-config.json',self.f.cfg)
        self.assertEqual(tcp.report(self.root,ew.metrics.assess)['status'],'NOT TESTED')

    def test_finite_baseline_without_migration_is_not_tested(self):
        state=json.loads(self.f.obj.path.read_text()); state['sessions']['baseline']=state['sessions'].pop('migration')
        v.save(self.f.obj.path,state)
        self.assertEqual(tcp.report(self.root,ew.metrics.assess)['status'],'NOT TESTED')

    def test_frozen_constructor_uses_saved_inventory_without_discovery(self):
        session=self.f.obj.session(); session['source_inventory']={'_meta':{'hostvars':{'network1':dict(ansible_host='network1',ansible_user='root')}}}
        self.f.obj.commit()
        with patch('ew_transport.subprocess.check_output') as discover:
            obj=ew.Workload(self.root,None)
            self.assertEqual(obj.transport.inventory,session['source_inventory']); discover.assert_not_called()


class OrderingTests(unittest.TestCase):
    def test_operator_checkpoint_templates_preserve_same_run_journal_and_boots(self):
        ansible=pathlib.Path(sys.executable).with_name('ansible-playbook')
        if not ansible.exists(): self.skipTest('Compatible offline Ansible is unavailable')
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); f=tcp_fixture(root)
            journal=(root/'network-mtu-plan.json').read_bytes()
            play=[dict(hosts='localhost',gather_facts=False,
                vars=dict(migration_run_dir=str(root),migration_run_id='same-run',ew_mtu_preparation_pause=False,
                    ansible_connection='local',ansible_python_interpreter=sys.executable),
                tasks=[{'ansible.builtin.include_tasks':str(ROOT/'playbooks/ew-mtu-preparation-tasks.yml')}])]
            (root/'checkpoint.yml').write_text(yaml.safe_dump(play,sort_keys=False))
            result=subprocess.run([str(ansible),'-i','localhost,',str(root/'checkpoint.yml')],
                text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout)
            checkpoint=json.loads((root/'ew-mtu-preparation.json').read_text())
            self.assertEqual(checkpoint['status'],'EXTERNAL_PREPARATION')
            self.assertEqual(checkpoint['original_guests'],f.obj.session()['guests'])
            self.assertEqual(checkpoint['network_journal'],json.loads(journal))
            self.assertEqual(checkpoint['run_directory'],str(root))
            self.assertEqual((root/'network-mtu-plan.json').read_bytes(),journal)
            self.assertFalse((root/'ew-mtu-preparation-ack.json').exists())

    def test_hook_is_after_all_worker_stops_before_db_and_has_no_openrc_or_sdk(self):
        plays=yaml.safe_load((ROOT/'playbooks/07-migrate-db.yml').read_text())
        stop=next(i for i,p in enumerate(plays) if 'Freeze all' in p['name'])
        activation=next(i for i,p in enumerate(plays) if 'dedicated TCP listener' in p['name'])
        db=next(i for i,p in enumerate(plays) if 'migration exactly once' in p['name'])
        self.assertLess(stop,activation); self.assertLess(activation,db)
        self.assertTrue(plays[stop]['any_errors_fatal']); self.assertTrue(plays[activation]['any_errors_fatal'])
        task=plays[activation]['tasks'][0]; self.assertIn('tcp-activate',task['ansible.builtin.command']['argv'])
        self.assertNotIn('ansible.builtin.shell',task)
        text=(ROOT/'scripts/ew_workload.py').read_text()
        self.assertLess(text.index("if args.action=='tcp-activate':"),text.index('import openstack'))

    def test_mtu_pause_is_after_network_target_verification_and_preserves_final_gates(self):
        phases=yaml.safe_load((ROOT/'playbooks/06-target-config.yml').read_text())
        tasks=phases[-1]['tasks']; names=[t['name'] for t in tasks]
        self.assertIn('match the saved MTU',names[1]); self.assertEqual(tasks[2]['ansible.builtin.include_tasks'],'ew-mtu-preparation-tasks.yml')
        helper=(ROOT/'playbooks/ew-mtu-preparation-tasks.yml').read_text()
        self.assertIn('ACKNOWLEDGED_NOT_VALIDATED',helper); self.assertIn("== 'continue'",helper)
        self.assertNotIn('ansible.builtin.shell',helper); self.assertNotIn('ansible.builtin.command',helper)
        tasks=[t for p in yaml.safe_load((ROOT/'playbooks/07-migrate-db.yml').read_text()) for t in p['tasks']]
        freeze=next(i for i,t in enumerate(tasks) if '--freeze-start' in t.get('ansible.builtin.command',{}).get('argv',[]))
        self.assertEqual(tasks[freeze-1]['ansible.builtin.command']['argv'][2],'ready')
        self.assertTrue(any('precutover-ready' in t.get('ansible.builtin.shell','') for t in tasks[:freeze]))
        self.assertTrue(any('ew_workload.py' in t.get('ansible.builtin.shell','') for t in tasks[:freeze]))


if __name__=='__main__': unittest.main()
