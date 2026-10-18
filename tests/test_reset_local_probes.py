"""Reset must release owned controller listeners and never signal a foreign PID."""
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import reset_local_probes as probes
from reset_local_probes import stop_probes
from run_layout import initialize, run_path


PROGRAM = '''import json, signal, socket, sys, time
from pathlib import Path
cfg = json.loads(Path(sys.argv[2]).read_text())
if cfg.get('ignore_term'):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if cfg.get('ignore_int'):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    sock.listen()
    Path(cfg['output']).write_text(str(sock.getsockname()[1]))
    while True:
        time.sleep(.05)
'''


class ResetLocalProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.backups = self.base/'backups'
        self.backups.mkdir()
        self.evidence = self.base/'local-probes-stop.json'
        self.script = self.base/'ns_probe.py'
        self.script.write_text(PROGRAM)
        # This executor exposes virtual child PIDs and a different /proc view.
        # Model that identity/descriptor boundary, but use real child listeners
        # and real SIGTERM through Popen for release/timeout behavior.
        self.children = {}
        self.identities = {}
        self.commands = {}
        original_read = Path.read_bytes
        def cmdline(path):
            if str(path).startswith('/proc/') and path.name == 'cmdline':
                pid = int(path.parts[2])
                return b'\0'.join(arg.encode() for arg in self.commands[pid])+b'\0'
            return original_read(path)
        def send(handle, sig):
            self.children[handle-10000].send_signal(sig)
        for target, replacement in (
            ('reset_local_probes.alive', self.live),
            ('reset_local_probes.identity', lambda pid: (
                dict(self.identities[pid]) if pid in self.children and
                self.children[pid].poll() is None else None)),
            ('reset_local_probes.process_commands', lambda: (
                (pid, list(args)) for pid, args in list(self.commands.items())
                if self.children[pid].poll() is None)),
            ('reset_local_probes.PROBE_SCRIPT', self.script),
            ('reset_local_probes.pidfd_open', lambda pid: pid+10000),
            ('reset_local_probes.pidfd_send_signal', send),
            ('reset_local_probes.close', lambda handle: None),
            ('pathlib.Path.read_bytes', cmdline),
        ):
            mocker = patch(target, replacement)
            mocker.start()
            self.addCleanup(mocker.stop)

    def live(self, saved):
        if not saved:
            return False
        pid = saved.get('pid')
        return (pid in self.children and self.children[pid].poll() is None and
                saved == self.identities[pid])

    def process(self, run='run-one', role='snat-server', ignore_term=False, script=None,
                fields=None, env=None, organized=False):
        root = self.backups/run
        if organized:
            initialize(root)
        (root/'ns').mkdir(parents=True, exist_ok=True)
        config = dict(run=run, console=False, output=str(root/'ns'/(role+'.jsonl')),
                      ignore_term=ignore_term)
        config.update(fields or {})
        config_path = root/'ns'/(role+'.json')
        config_path.write_text(json.dumps(config))
        args = [sys.executable, str(script or self.script),
                'serve' if role=='snat-server' else 'client', str(config_path)]
        process = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children[process.pid] = process
        self.identities[process.pid] = dict(pid=process.pid, start_ticks=str(time.monotonic_ns()), boot='test-boot')
        self.commands[process.pid] = args
        def cleanup():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(.3)
                except subprocess.TimeoutExpired:
                    process.kill()
            process.wait()
        self.addCleanup(cleanup)
        deadline = time.monotonic()+3
        while not Path(config['output']).exists():
            if process.poll() is not None or time.monotonic() > deadline:
                self.fail('Test listener did not start')
            time.sleep(.01)
        checkpoint = run_path(root,'extended-resources.json')
        state = json.loads(checkpoint.read_text()) if checkpoint.exists() else dict(schema=1, run=run, processes={})
        state['processes'][role] = dict(status='RUNNING', config=config, identity=dict(self.identities[process.pid]))
        checkpoint.write_text(json.dumps(state))
        run_path(root,'validation-config.json').write_text(json.dumps({'run': run}))
        return process, root, state

    def test_stops_both_owned_probes_releases_listeners_and_preserves_evidence(self):
        server, root, _ = self.process()
        client, _, _ = self.process(role='fip-client')
        checkpoint = (root/'extended-resources.json').read_bytes()
        ports = [int((root/'ns'/(role+'.jsonl')).read_text()) for role in ('snat-server', 'fip-client')]
        result = stop_probes(self.backups, self.evidence)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual({r['role'] for r in result['stopped']}, {'snat-server', 'fip-client'})
        server.wait(2)
        client.wait(2)
        self.assertEqual((root/'extended-resources.json').read_bytes(), checkpoint)
        for port in ports:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', port))
        self.assertEqual(json.loads(self.evidence.read_text()), result)

    def test_empty_backup_tree_is_valid(self):
        result = stop_probes(self.backups, self.evidence)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['stopped'], [])

    def test_reset_releases_listeners_from_flat_and_organized_runs_together(self):
        flat,old,_=self.process(run='flat-run')
        organized,new,_=self.process(run='organized-run',organized=True)
        checkpoints={p:p.read_bytes() for p in
            (run_path(old,'extended-resources.json'),run_path(new,'extended-resources.json'))}
        ports=[int((root/'ns/snat-server.jsonl').read_text()) for root in (old,new)]
        result=stop_probes(self.backups,self.evidence)
        self.assertEqual({row['run'] for row in result['stopped']},{'flat-run','organized-run'})
        flat.wait(2); organized.wait(2)
        for path,data in checkpoints.items(): self.assertEqual(path.read_bytes(),data)
        for port in ports:
            with socket.socket() as sock: sock.bind(('127.0.0.1',port))
        self.assertEqual(stop_probes(self.backups,self.evidence)['stopped'],[])

    def test_invalid_organized_owner_blocks_signals_to_the_valid_flat_run(self):
        flat,_,_=self.process(run='a-flat')
        organized,root,state=self.process(run='z-organized',organized=True)
        state['processes']['snat-server']['config']['run']='foreign'
        run_path(root,'extended-resources.json').write_text(json.dumps(state))
        with self.assertRaisesRegex(RuntimeError,'config ownership'):
            stop_probes(self.backups,self.evidence)
        self.assertIsNone(flat.poll()); self.assertIsNone(organized.poll())
        self.assertEqual(json.loads(self.evidence.read_text())['signaled'],[])

    def test_stale_start_ticks_never_signal_reused_pid(self):
        process, root, state = self.process()
        state['processes']['snat-server']['identity']['start_ticks'] += '0'
        (root/'extended-resources.json').write_text(json.dumps(state))
        result = stop_probes(self.backups, self.evidence)
        self.assertEqual(result['stopped'], [])
        self.assertIsNone(process.poll())

    def test_missing_checkpoint_listener_is_discovered_stopped_and_not_recheckpointed(self):
        for organized in (False, True):
            with self.subTest(organized=organized):
                process, root, _ = self.process(run='orphan-'+str(organized), organized=organized)
                checkpoint = run_path(root, 'extended-resources.json')
                checkpoint.unlink()
                config = (root/'ns/snat-server.json').read_bytes()
                port = int((root/'ns/snat-server.jsonl').read_text())
                result = stop_probes(self.backups, self.evidence)
                process.wait(2)
                self.assertEqual(result['status'], 'PASS')
                self.assertEqual(result['stopped'][0]['identity']['pid'], process.pid)
                self.assertEqual(result['stopped'][0]['source'], 'live-command-without-checkpoint')
                self.assertEqual(result['discovered'][0]['config_file_present'], True)
                self.assertEqual(result['discovered'][0]['config_path'], str(root/'ns/snat-server.json'))
                self.assertFalse(checkpoint.exists())
                self.assertEqual((root/'ns/snat-server.json').read_bytes(), config)
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', port))

    def test_deleted_entire_run_still_stops_its_exact_project_command(self):
        process, root, _ = self.process()
        shutil.rmtree(root)
        result = stop_probes(self.backups, self.evidence)
        process.wait(2)
        self.assertEqual(result['discovered'][0]['config_file_present'], False)
        self.assertEqual(len(result['stopped']), 1)
        self.assertFalse(root.exists())
        self.assertEqual(stop_probes(self.backups, self.evidence)['discovered'], [])

    def test_orphan_discovery_is_recorded_before_the_first_signal(self):
        process, root, _ = self.process()
        run_path(root, 'extended-resources.json').unlink()
        sender = probes.pidfd_send_signal
        def send(handle, sig):
            evidence = json.loads(self.evidence.read_text())
            self.assertEqual(evidence['status'], 'IN_PROGRESS')
            self.assertEqual(evidence['discovered'][0]['identity']['pid'], process.pid)
            self.assertEqual(evidence['signaled'], [])
            sender(handle, sig)
        with patch.object(probes, 'pidfd_send_signal', side_effect=send):
            stop_probes(self.backups, self.evidence)
        process.wait(2)

    def test_invalid_orphan_config_blocks_all_signals(self):
        owned, _, _ = self.process(run='a-owned')
        orphan, root, _ = self.process(run='z-orphan')
        run_path(root, 'extended-resources.json').unlink()
        path = root/'ns/snat-server.json'
        cfg = json.loads(path.read_text())
        cfg['run'] = 'foreign'
        path.write_text(json.dumps(cfg))
        with self.assertRaisesRegex(RuntimeError, 'orphan N-S config ownership'):
            stop_probes(self.backups, self.evidence)
        self.assertIsNone(owned.poll())
        self.assertIsNone(orphan.poll())
        self.assertEqual(json.loads(self.evidence.read_text())['signaled'], [])

    def test_invalid_orphan_validation_run_blocks_signals(self):
        process, root, _ = self.process()
        run_path(root, 'extended-resources.json').unlink()
        run_path(root, 'validation-config.json').write_text(json.dumps({'run': 'foreign'}))
        with self.assertRaisesRegex(RuntimeError, 'orphan N-S validation run mismatch'):
            stop_probes(self.backups, self.evidence)
        self.assertIsNone(process.poll())

    def test_existing_checkpoint_in_other_layout_never_authorizes_discovery(self):
        process, root, state = self.process(organized=True)
        run_path(root, 'extended-resources.json').unlink()
        (root/'extended-resources.json').write_text(json.dumps(state))
        result = stop_probes(self.backups, self.evidence)
        self.assertEqual(result['discovered'], [])
        self.assertEqual(result['signaled'], [])
        self.assertIsNone(process.poll())

    def test_other_checkout_with_the_same_probe_filename_is_ignored_without_checkpoint(self):
        foreign = self.base/'foreign/ns_probe.py'
        foreign.parent.mkdir()
        foreign.write_text(PROGRAM)
        process, root, _ = self.process(script=foreign)
        run_path(root, 'extended-resources.json').unlink()
        result = stop_probes(self.backups, self.evidence)
        self.assertEqual(result['signaled'], [])
        self.assertIsNone(process.poll())

    def test_probe_config_outside_selected_backup_root_is_ignored(self):
        process, _, _ = self.process()
        result = stop_probes(self.base/'different-backup-root', self.evidence)
        self.assertEqual(result['signaled'], [])
        self.assertIsNone(process.poll())

    def test_config_role_must_match_action_for_an_orphan(self):
        process, root, _ = self.process()
        run_path(root, 'extended-resources.json').unlink()
        self.commands[process.pid][-2] = 'client'
        result = stop_probes(self.backups, self.evidence)
        self.assertEqual(result['signaled'], [])
        self.assertIsNone(process.poll())

    def test_identity_change_while_opening_orphan_pidfd_is_not_signaled(self):
        process, root, _ = self.process()
        run_path(root, 'extended-resources.json').unlink()
        def opened(pid):
            self.identities[pid]['start_ticks'] += '0'
            return pid+10000
        with patch.object(probes, 'pidfd_open', side_effect=opened):
            result = stop_probes(self.backups, self.evidence)
        self.assertEqual(result['signaled'], [])
        self.assertEqual(result['skipped'][0]['reason'], 'process identity changed')
        self.assertIsNone(process.poll())

    def test_changed_config_cannot_authorize_a_signal(self):
        process, root, state = self.process()
        state['processes']['snat-server']['config']['run'] = 'foreign'
        (root/'extended-resources.json').write_text(json.dumps(state))
        with self.assertRaisesRegex(RuntimeError, 'config ownership'):
            stop_probes(self.backups, self.evidence)
        self.assertIsNone(process.poll())
        self.assertEqual(json.loads(self.evidence.read_text())['status'], 'FAIL')

    def test_foreign_command_blocks_all_signals_before_mutation(self):
        owned, _, _ = self.process(run='a-owned')
        foreign_script = self.base/'foreign.py'
        foreign_script.write_text(PROGRAM)
        foreign, _, _ = self.process(run='z-foreign', script=foreign_script)
        with self.assertRaisesRegex(RuntimeError, 'command ownership'):
            stop_probes(self.backups, self.evidence)
        self.assertIsNone(owned.poll())
        self.assertIsNone(foreign.poll())
        self.assertEqual(json.loads(self.evidence.read_text())['stopped'], [])

    def test_unknown_live_role_is_not_stopped(self):
        process, _, _ = self.process(role='foreign-role')
        with self.assertRaisesRegex(RuntimeError, 'Unknown live'):
            stop_probes(self.backups, self.evidence)
        self.assertIsNone(process.poll())

    def test_ignored_sigterm_blocks_destroy_without_force_kill(self):
        process, _, state = self.process(ignore_term=True)
        saved = state['processes']['snat-server']['identity']
        with self.assertRaisesRegex(RuntimeError, 'did not stop'):
            stop_probes(self.backups, self.evidence, timeout=.1)
        self.assertTrue(self.live(saved))
        self.assertIsNone(process.poll())
        evidence = json.loads(self.evidence.read_text())
        self.assertEqual(evidence['status'], 'FAIL')
        self.assertEqual(evidence['stopped'], [])
        self.assertEqual(evidence['remaining'][0]['identity'], saved)
        self.assertEqual(evidence['signaled'][0]['signals'][0]['signal'], 'SIGTERM')

    def real_client(self, legacy=False, with_server=False):
        sockets = [socket.socket() for _ in range(3)]
        try:
            for sock in sockets:
                sock.bind(('127.0.0.1', 0))
            old, new, control = [sock.getsockname()[1] for sock in sockets]
        finally:
            for sock in sockets:
                sock.close()
        fake_bin = self.base/'bin'
        fake_bin.mkdir()
        marker = self.base/'ping-stopped'
        ping = fake_bin/'ping'
        ping.write_text('#!'+sys.executable+'\n'+'''import os, signal, time
from pathlib import Path
stopped = False
def stop(*_):
    global stopped
    stopped = True
signal.signal(signal.SIGTERM, stop)
while not stopped:
    print('[1.000000] 64 bytes from 127.0.0.1: icmp_seq=1 ttl=64 time=0.1 ms', flush=True)
    time.sleep(.01)
Path(os.environ['PING_STOP_MARKER']).write_text('reaped by probe cleanup')
''')
        ping.chmod(0o700)
        cfg = dict(bind='127.0.0.1', source='127.0.0.1', target='127.0.0.1', port=old,
            new_port=new, control_port=control, control_token='test-token',
            expected_peer='127.0.0.1', expected_server='server', server='server', label='fip',
            interval=.03, timeout=.15, lifetime=30, icmp_enabled=True,
            icmp_output=str(self.base/'sender.jsonl'))
        script = ROOT/'scripts/ns_probe.py'
        if legacy:
            source = script.read_text()
            old_loop = "while time.monotonic()-started < self.cfg['lifetime']:"
            fixed_loop = "while not self.stop.is_set() and time.monotonic()-started < self.cfg['lifetime']:"
            self.assertEqual(source.count(fixed_loop), 2)
            source = source.replace(fixed_loop, old_loop, 1)
            source = source.replace('            signal.signal(signal.SIGINT, lambda *_: self.stop.set())\n', '', 1)
            self.script.write_text(source)
            script = self.script
        if with_server:
            from ns_probe import Server
            server = Server(dict(cfg, run='run-one', console=False))
            def stop_server():
                for _, listener in list(server.listeners.values()):
                    listener.shutdown()
                    listener.server_close()
            self.addCleanup(stop_server)
        env = dict(os.environ, PATH=str(fake_bin)+os.pathsep+os.environ['PATH'],
                   PYTHONPATH=str(ROOT/'scripts'), PING_STOP_MARKER=str(marker))
        process, root, state = self.process(role='fip-client', fields=cfg, script=script, env=env)
        deadline = time.monotonic()+3
        while not (self.base/'sender.jsonl').exists() or '"kind": "reply"' not in (self.base/'sender.jsonl').read_text():
            if process.poll() is not None or time.monotonic() > deadline:
                self.fail('Production ping reader did not start')
            time.sleep(.01)
        return process, root, cfg, marker

    def test_production_client_term_exits_and_reaps_ping_even_when_target_is_down(self):
        process, _, cfg, marker = self.real_client()
        result = stop_probes(self.backups, self.evidence, timeout=2, interrupt_legacy_client=True)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(process.wait(2), 0)
        self.assertTrue(marker.exists(), 'Original ping child was not stopped')
        self.assertEqual([s['signal'] for s in result['stopped'][0]['signals']], ['SIGTERM'])
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', cfg['control_port']))

    def test_legacy_client_reproduces_timeout_then_interrupt_runs_ping_cleanup(self):
        process, _, cfg, marker = self.real_client(legacy=True)
        with self.assertRaisesRegex(RuntimeError, 'did not stop.*fip-client'):
            stop_probes(self.backups, self.evidence, timeout=.15)
        self.assertIsNone(process.poll())
        self.assertFalse(marker.exists())
        failed = json.loads(self.evidence.read_text())
        self.assertEqual(failed['stopped'], [])
        self.assertEqual(failed['remaining'][0]['role'], 'fip-client')
        result = stop_probes(self.backups, self.evidence, timeout=.5, interrupt_legacy_client=True)
        self.assertEqual(result['status'], 'PASS')
        self.assertIsNotNone(process.poll())
        self.assertTrue(marker.exists(), 'Interrupt must run legacy cleanup, including its ping')
        self.assertEqual([s['signal'] for s in result['stopped'][0]['signals']], ['SIGTERM', 'SIGINT'])
        self.assertEqual(result['remaining'], [])
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', cfg['control_port']))

    def test_orphan_legacy_client_interrupt_still_reaps_its_ping(self):
        process, root, cfg, marker = self.real_client(legacy=True)
        run_path(root, 'extended-resources.json').unlink()
        result = stop_probes(self.backups, self.evidence, timeout=.5, interrupt_legacy_client=True)
        process.wait(2)
        self.assertTrue(marker.exists())
        self.assertEqual(result['stopped'][0]['source'], 'live-command-without-checkpoint')
        self.assertEqual([s['signal'] for s in result['stopped'][0]['signals']], ['SIGTERM', 'SIGINT'])
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', cfg['control_port']))

    def test_orphan_client_changed_to_another_checkout_is_never_interrupted(self):
        process, root, _ = self.process(role='fip-client', ignore_term=True)
        run_path(root, 'extended-resources.json').unlink()
        sender = probes.pidfd_send_signal
        def send(handle, sig):
            sender(handle, sig)
            self.commands[process.pid][-3] = str(self.base/'foreign/ns_probe.py')
        with patch.object(probes, 'pidfd_send_signal', side_effect=send):
            with self.assertRaisesRegex(RuntimeError, 'command ownership'):
                stop_probes(self.backups, self.evidence, timeout=.1, interrupt_legacy_client=True)
        self.assertIsNone(process.poll())
        evidence = json.loads(self.evidence.read_text())
        self.assertEqual([s['signal'] for s in evidence['signaled'][0]['signals']], ['SIGTERM'])

    def test_production_client_with_held_and_both_listeners_stops_without_restarting_ping(self):
        from ns_control import request
        process, root, cfg, marker = self.real_client(with_server=True)
        deadline = time.monotonic()+3
        payload = dict(ip='127.0.0.1', port=cfg['control_port'], run='run-one', token=cfg['control_token'])
        while True:
            try:
                checked = request(dict(payload, action='CHECK'))
                break
            except (RuntimeError, OSError):
                if time.monotonic() >= deadline:
                    self.fail('Production held/control sockets did not become ready')
                time.sleep(.01)
        ack = request(dict(payload, action='ARM'))
        self.assertTrue(ack['armed'])
        self.assertEqual(checked['connection_attempts'], 1)
        result = stop_probes(self.backups, self.evidence, timeout=2, interrupt_legacy_client=True)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(process.wait(2), 0)
        self.assertTrue(marker.exists())
        events = [json.loads(line) for line in (self.base/'sender.jsonl').read_text().splitlines()]
        self.assertEqual([event['kind'] for event in events].count('start'), 1)
        self.assertEqual([event['kind'] for event in events].count('end'), 1)
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', cfg['control_port']))

    def test_legacy_interrupt_never_targets_server(self):
        process, _, _ = self.process(ignore_term=True)
        with self.assertRaisesRegex(RuntimeError, 'did not stop'):
            stop_probes(self.backups, self.evidence, timeout=.1, interrupt_legacy_client=True)
        evidence = json.loads(self.evidence.read_text())
        self.assertEqual([s['signal'] for s in evidence['signaled'][0]['signals']], ['SIGTERM'])
        self.assertIsNone(process.poll())

    def test_client_ignoring_both_signals_still_blocks_destroy_without_kill(self):
        process, _, _ = self.process(role='fip-client', ignore_term=True, fields=dict(ignore_int=True))
        with self.assertRaisesRegex(RuntimeError, 'did not stop'):
            stop_probes(self.backups, self.evidence, timeout=.1, interrupt_legacy_client=True)
        evidence = json.loads(self.evidence.read_text())
        self.assertEqual(evidence['status'], 'FAIL')
        self.assertEqual(evidence['stopped'], [])
        self.assertEqual(evidence['remaining'][0]['identity']['pid'], process.pid)
        self.assertEqual([s['signal'] for s in evidence['signaled'][0]['signals']], ['SIGTERM', 'SIGINT'])
        self.assertIsNone(process.poll())

    def test_changed_command_after_term_blocks_legacy_interrupt(self):
        process, _, _ = self.process(role='fip-client', ignore_term=True)
        original_send = probes.pidfd_send_signal
        def change_command(handle, sig):
            original_send(handle, sig)
            if sig == signal.SIGTERM:
                self.commands[process.pid][-3] = '/foreign.py'
        with patch.object(probes, 'pidfd_send_signal', side_effect=change_command):
            with self.assertRaisesRegex(RuntimeError, 'command ownership'):
                stop_probes(self.backups, self.evidence, timeout=.1, interrupt_legacy_client=True)
        self.assertIsNone(process.poll())
        evidence = json.loads(self.evidence.read_text())
        self.assertEqual([s['signal'] for s in evidence['signaled'][0]['signals']], ['SIGTERM'])

    def test_repeated_stop_after_success_is_idempotent(self):
        process, _, _ = self.process()
        stop_probes(self.backups, self.evidence)
        process.wait(2)
        again = stop_probes(self.backups, self.evidence, interrupt_legacy_client=True)
        self.assertEqual(again['status'], 'PASS')
        self.assertEqual(again['signaled'], [])
        self.assertEqual(again['remaining'], [])


if __name__ == '__main__':
    unittest.main()
