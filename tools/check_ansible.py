#!/usr/bin/env python3
"""Syntax-check every entrypoint/transport with a local fixture inventory."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    entries = [ROOT/'migrate-to-ovn.yml', ROOT/'reset-lab-to-ovs.yml']
    for directory, pattern in (('playbooks/recovery', 'resume-*.yml'),
                               ('archive/playbooks', '*.yml'), ('playbooks/transport', '*.yml')):
        entries.extend(sorted((ROOT/directory).glob(pattern)))
    failures = []
    for path in entries:
        result = subprocess.run([sys.executable, '-m', 'ansible.cli.playbook', '-i',
            str(ROOT/'tests/fixtures/syntax-inventory.ini'), '--syntax-check', str(path)],
            cwd=ROOT, capture_output=True, text=True)
        print(('PASS ' if result.returncode == 0 else 'FAIL ')+str(path.relative_to(ROOT)), flush=True)
        if result.returncode:
            failures.append(path)
            print(result.stdout+result.stderr, file=sys.stderr)
    print(f'{len(entries)-len(failures)}/{len(entries)} entrypoints passed')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
