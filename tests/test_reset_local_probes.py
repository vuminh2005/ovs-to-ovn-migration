"""Reset must release owned controller listeners and never signal a foreign PID."""
import json
from pathlib import Path
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


PROGRAM = '''import json, signal, socket, sys, time
from pathlib import Path
cfg = json.loads(Path(sys.argv[2]).read_text())
if cfg.get('ignore_term'):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
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

    def process(self, run='run-one', role='snat-server', ignore_term=False, script=None):
        root = self.backups/run
        (root/'ns').mkdir(parents=True, exist_ok=True)
        config = dict(run=run, console=False, output=str(root/'ns'/(role+'.jsonl')),
                      ignore_term=ignore_term)
        config_path = root/'ns'/(role+'.json')
        config_path.write_text(json.dumps(config))
        args = [sys.executable, str(script or self.script),
                'serve' if role=='snat-server' else 'client', str(config_path)]
        process = subprocess.Popen(args)
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
        checkpoint = root/'extended-resources.json'
        state = json.loads(checkpoint.read_text()) if checkpoint.exists() else dict(schema=1, run=run, processes={})
        state['processes'][role] = dict(status='RUNNING', config=config, identity=dict(self.identities[process.pid]))
        checkpoint.write_text(json.dumps(state))
        (root/'validation-config.json').write_text(json.dumps({'run': run}))
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

    def test_stale_start_ticks_never_signal_reused_pid(self):
        process, root, state = self.process()
        state['processes']['snat-server']['identity']['start_ticks'] += '0'
        (root/'extended-resources.json').write_text(json.dumps(state))
        result = stop_probes(self.backups, self.evidence)
        self.assertEqual(result['stopped'], [])
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
        self.assertEqual(json.loads(self.evidence.read_text())['status'], 'FAIL')


if __name__ == '__main__':
    unittest.main()
