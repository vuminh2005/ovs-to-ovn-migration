#!/usr/bin/env python3
"""Stop checkpointed controller N-S probes before a confirmed destructive reset."""
import argparse
import json
import math
import os
from os import close, pidfd_open
from pathlib import Path
import signal
from signal import pidfd_send_signal
import time

from dataplane_capture import alive, save
from run_layout import run_path


def command_owned(pid, role, config_path):
    args = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    args = [os.fsdecode(arg) for arg in args if arg]
    action = 'serve' if role == 'snat-server' else 'client'
    if (len(args) < 4 or Path(args[-3]).name != 'ns_probe.py' or
        args[-2] != action or Path(args[-1]).resolve() != config_path):
        raise RuntimeError('Live N-S command ownership mismatch for '+str(pid))


def stop_probes(backup_root, evidence_path, timeout=5, *, interrupt_legacy_client=False):
    backup_root = Path(backup_root).resolve()
    evidence_path = Path(evidence_path)
    result = dict(status='IN_PROGRESS', started_at=time.time(), timeout_seconds=timeout,
                  interrupt_legacy_client=interrupt_legacy_client,
                  signaled=[], stopped=[], remaining=[], skipped=[])
    owned = []
    handles = []
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
                pid = saved['pid']
                try:
                    handle = pidfd_open(pid)
                except ProcessLookupError:
                    result['skipped'].append(dict(run=root.name, role=role, reason='process ended during inspection'))
                    continue
                handles.append(handle)
                if not alive(saved):
                    result['skipped'].append(dict(run=root.name, role=role, reason='process identity changed'))
                    continue
                command_owned(pid, role, config_path)
                owned.append((handle, dict(run=root.name, role=role, identity=saved, signals=[]), config_path))
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
                command_owned(item['identity']['pid'], item['role'], config)
            for handle, item, _ in pending:
                send(handle, item, signal.SIGINT)
            if pending:
                wait_for_exit()
        remaining = [item for _, item, _ in owned if alive(item['identity'])]
        if remaining:
            details = ', '.join(f"{item['run']}/{item['role']} PID {item['identity']['pid']}"
                                for item in remaining)
            raise RuntimeError('Checkpointed N-S probe did not stop: '+details+
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
