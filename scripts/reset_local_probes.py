#!/usr/bin/env python3
"""Stop checkpointed controller N-S probes before a confirmed destructive reset."""
import argparse
import json
import os
from os import close, pidfd_open
from pathlib import Path
import signal
from signal import pidfd_send_signal
import time

from dataplane_capture import alive, save


def stop_probes(backup_root, evidence_path, timeout=5):
    backup_root = Path(backup_root).resolve()
    evidence_path = Path(evidence_path)
    result = dict(status='IN_PROGRESS', stopped=[], skipped=[])
    owned = []
    handles = []
    try:
        # Validate every live candidate before signaling any of them. pidfds
        # keep signals bound to the original process even if a PID is reused.
        for checkpoint in sorted(backup_root.glob('*/extended-resources.json')):
            root = checkpoint.parent
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
                cfg = json.loads((root/'validation-config.json').read_text())
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
                args = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
                args = [os.fsdecode(arg) for arg in args if arg]
                action = 'serve' if role == 'snat-server' else 'client'
                if (len(args) < 4 or Path(args[-3]).name != 'ns_probe.py' or
                    args[-2] != action or Path(args[-1]).resolve() != config_path):
                    raise RuntimeError('Live N-S command ownership mismatch for '+str(pid))
                owned.append((handle, saved, root.name, role))
        for handle, saved, run, role in owned:
            if not alive(saved):
                continue
            try:
                pidfd_send_signal(handle, signal.SIGTERM)
            except ProcessLookupError:
                continue
            result['stopped'].append(dict(run=run, role=role, identity=saved))
        deadline = time.monotonic()+timeout
        while any(alive(saved) for _, saved, _, _ in owned):
            if time.monotonic() >= deadline:
                raise RuntimeError('Checkpointed N-S probe did not stop; Kolla destroy must not proceed')
            time.sleep(.05)
        result['status'] = 'PASS'
    except Exception as exc:
        result.update(status='FAIL', error=str(exc))
        raise
    finally:
        for handle in handles:
            close(handle)
        save(evidence_path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup_root', type=Path)
    parser.add_argument('evidence_path', type=Path)
    args = parser.parse_args()
    print(json.dumps(stop_probes(args.backup_root, args.evidence_path), sort_keys=True))


if __name__ == '__main__':
    main()
