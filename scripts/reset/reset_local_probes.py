#!/usr/bin/env python3
"""Stop owned controller N-S probes before a confirmed destructive reset."""

# Source helpers are grouped; remote/guest copies keep flat sibling imports.
import sys as _sys
from pathlib import Path as _Path
_source = _Path(__file__).resolve().parents[1]
if _source.name == 'scripts' and (_source / '_paths.py').is_file():
    _sys.path.insert(0, str(_source))
    from _paths import configure as _configure
    _configure()

from _paths import PROJECT_ROOT, script_path
import argparse
import json
import math
import os
from os import close, pidfd_open
from pathlib import Path
import signal
from signal import pidfd_send_signal
import time

from dataplane_capture import alive, identity, save
from run_layout import run_path


PROBE_SCRIPT = script_path('ns_probe.py').resolve()
# Reset must still recognize a probe launched by this checkout's former layout,
# even when its run/checkpoint was removed before the checkout was upgraded.
LEGACY_PROBE_SCRIPT = (PROJECT_ROOT/'scripts/ns_probe.py').resolve()


def process_command(pid):
    args = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    return [os.fsdecode(arg) for arg in args if arg]


def process_commands():
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            yield int(proc.name), process_command(int(proc.name))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue


def command_owned(pid, role, config_path, *, exact_script=False):
    args = process_command(pid)
    action = 'serve' if role == 'snat-server' else 'client'
    if (len(args) < 4 or Path(args[-3]).name != 'ns_probe.py' or
        args[-2] != action or Path(args[-1]).resolve() != config_path):
        raise RuntimeError('Live N-S command ownership mismatch for '+str(pid))
    if exact_script and (len(args) != 4 or not Path(args[-3]).is_absolute() or
                         Path(args[-3]).resolve() not in (PROBE_SCRIPT, LEGACY_PROBE_SCRIPT)):
        raise RuntimeError('Live N-S command ownership mismatch for '+str(pid))


def orphan_config(args, backup_root):
    """Recognize only this checkout's direct Python probe invocation."""
    if (len(args) != 4 or Path(args[-3]).name != 'ns_probe.py' or not Path(args[-3]).is_absolute() or
            Path(args[-3]).resolve() not in (PROBE_SCRIPT, LEGACY_PROBE_SCRIPT)):
        return None
    role = {'serve': 'snat-server', 'client': 'fip-client'}.get(args[-2])
    path = Path(args[-1])
    if role is None or not path.is_absolute():
        return None
    config_path = path.resolve()
    try:
        parts = config_path.relative_to(backup_root).parts
    except ValueError:
        return None
    if len(parts) != 3 or parts[1:] != ('ns', role+'.json'):
        return None
    if path != config_path:
        raise RuntimeError('Non-canonical live N-S config path: '+str(path))
    return backup_root/parts[0], role, config_path


def stop_probes(backup_root, evidence_path, timeout=5, *, interrupt_legacy_client=False):
    backup_root = Path(backup_root).resolve()
    evidence_path = Path(evidence_path)
    result = dict(status='IN_PROGRESS', started_at=time.time(), timeout_seconds=timeout,
                  backup_root=str(backup_root), discovered=[],
                  interrupt_legacy_client=interrupt_legacy_client,
                  signaled=[], stopped=[], remaining=[], skipped=[])
    owned = []
    handles = []
    def register(saved, root, role, config_path, source):
        pid = saved['pid']
        try:
            handle = pidfd_open(pid)
        except ProcessLookupError:
            result['skipped'].append(dict(run=root.name, role=role, reason='process ended during inspection'))
            return
        handles.append(handle)
        if not alive(saved):
            result['skipped'].append(dict(run=root.name, role=role, reason='process identity changed'))
            return
        command_owned(pid, role, config_path, exact_script=source != 'checkpoint')
        item = dict(run=root.name, role=role, identity=saved, source=source, signals=[])
        owned.append((handle, item, config_path))
        if source != 'checkpoint':
            result['discovered'].append(dict(item, config_path=str(config_path),
                config_file_present=config_path.is_file()))

    def wait_for_exit():
        deadline = time.monotonic()+timeout
        while any(alive(item['identity']) for _, item, _ in owned):
            if time.monotonic() >= deadline:
                return
            time.sleep(.05)

    def send(handle, item, sig):
        if not alive(item['identity']):
            return
        try:
            pidfd_send_signal(handle, sig)
        except ProcessLookupError:
            return
        if not item['signals']:
            result['signaled'].append(item)
        item['signals'].append(dict(signal=sig.name, sent_at=time.time()))

    try:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('Probe stop timeout must be finite and positive')
        # Validate every live candidate before signaling any of them. pidfds
        # keep signals bound to the original process even if a PID is reused.
        for root in sorted(backup_root.iterdir()) if backup_root.is_dir() else []:
            if not root.is_dir() or root.is_symlink():
                continue
            checkpoint = run_path(root, 'extended-resources.json')
            if not checkpoint.is_file():
                continue
            state = json.loads(checkpoint.read_text())
            for role, cp in state.get('processes', {}).items():
                saved = cp.get('identity')
                if not alive(saved):
                    result['skipped'].append(dict(run=root.name, role=role, reason='not the original live process'))
                    continue
                if state.get('schema') != 1 or state.get('run') != root.name:
                    raise RuntimeError('Live N-S checkpoint run/schema mismatch: '+str(checkpoint))
                if role not in ('snat-server', 'fip-client'):
                    raise RuntimeError('Unknown live N-S process role: '+role)
                cfg = json.loads(run_path(root, 'validation-config.json').read_text())
                config_path = root/'ns'/(role+'.json')
                process_cfg = json.loads(config_path.read_text())
                if (cfg.get('run') != root.name or process_cfg.get('run') != root.name or
                    cp.get('config') != process_cfg or process_cfg.get('console') is not False or
                    process_cfg.get('output') != str(root/'ns'/(role+'.jsonl'))):
                    raise RuntimeError('Live N-S config ownership mismatch: '+str(config_path))
                register(saved, root, role, config_path, 'checkpoint')
        # A deleted checkpoint must not leave this project's probe running
        # forever. For reset only, current exact argv establishes scope and a
        # fresh identity/pidfd binds the signal to that inspected process. This
        # never repairs a checkpoint or authorizes migration resume.
        for pid, args in process_commands():
            candidate = orphan_config(args, backup_root)
            if candidate is None:
                continue
            root, role, config_path = candidate
            if any(path.exists() for path in {
                    run_path(root, 'extended-resources.json'),
                    root/'extended-resources.json',
                    root/'checkpoints/extended-resources.json'}):
                # A stale/reused identity in an existing checkpoint must not
                # be reclassified as an orphan and bypass its identity guard.
                continue
            saved = identity(pid)
            if not alive(saved):
                continue
            if config_path.exists():
                cfg = json.loads(config_path.read_text())
                if (cfg.get('run') != root.name or cfg.get('console') is not False or
                        cfg.get('output') != str(root/'ns'/(role+'.jsonl'))):
                    raise RuntimeError('Live orphan N-S config ownership mismatch: '+str(config_path))
            validation = run_path(root, 'validation-config.json')
            if validation.exists() and json.loads(validation.read_text()).get('run') != root.name:
                raise RuntimeError('Live orphan N-S validation run mismatch: '+str(validation))
            register(saved, root, role, config_path, 'live-command-without-checkpoint')
        # Preserve discovery evidence before stopping a process whose original
        # checkpoint has disappeared. Validate all candidates before any signal.
        save(evidence_path, result)
        for handle, item, _ in owned:
            send(handle, item, signal.SIGTERM)
        wait_for_exit()
        if interrupt_legacy_client:
            # Old clients set the stop event on TERM but never test it in their
            # main loop. INT raises KeyboardInterrupt and runs their existing
            # finally block, including stopping/reaping the original ping.
            # Never force-kill a parent and leave its ping child behind.
            pending = [(handle, item, config) for handle, item, config in owned
                       if item['role'] == 'fip-client' and alive(item['identity'])]
            for _, item, config in pending:
                command_owned(item['identity']['pid'], item['role'], config,
                              exact_script=item['source'] != 'checkpoint')
            for handle, item, _ in pending:
                send(handle, item, signal.SIGINT)
            if pending:
                wait_for_exit()
        remaining = [item for _, item, _ in owned if alive(item['identity'])]
        if remaining:
            details = ', '.join(f"{item['run']}/{item['role']} PID {item['identity']['pid']}"
                                for item in remaining)
            raise RuntimeError('Owned N-S probe did not stop: '+details+
                               '; Kolla destroy must not proceed. Evidence: '+str(evidence_path))
        result['status'] = 'PASS'
    except Exception as exc:
        result.update(status='FAIL', error=str(exc))
        raise
    finally:
        result['stopped'] = [item for item in result['signaled'] if not alive(item['identity'])]
        result['remaining'] = [item for _, item, _ in owned if alive(item['identity'])]
        result['finished_at'] = time.time()
        for handle in handles:
            close(handle)
        save(evidence_path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup_root', type=Path)
    parser.add_argument('evidence_path', type=Path)
    parser.add_argument('--timeout', type=float, default=5,
                        help='Seconds to wait after each signal stage (default: 5)')
    parser.add_argument('--interrupt-legacy-client', action='store_true',
                        help='If TERM times out, interrupt only owned fip-client parents to run their cleanup')
    args = parser.parse_args()
    print(json.dumps(stop_probes(args.backup_root, args.evidence_path, args.timeout,
                                interrupt_legacy_client=args.interrupt_legacy_client), sort_keys=True))


if __name__ == '__main__':
    main()
