#!/usr/bin/env python3
"""One persistent ping per flow and boot/process heartbeats for added endpoints."""
import json
from pathlib import Path
import subprocess
import time


def main():
    cfg = json.loads(Path('/etc/aux-ping.json').read_text())
    process = None
    if cfg.get('peer'):
        process = subprocess.Popen(['ping', '-n', '-I', cfg['ip'], '-i', str(cfg['interval']), cfg['peer']],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started = time.monotonic()
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    while time.monotonic()-started < cfg['lifetime']:
        row = dict(run=cfg['run'], vm=cfg['vm'], boot=boot, mono=time.monotonic(),
                   ping_alive=process is None or process.poll() is None)
        with open('/dev/ttyS0', 'w') as console:
            console.write('OVN_AUX_JSON '+json.dumps(row)+'\n')
        if process is not None and process.poll() is not None:
            raise SystemExit('Persistent ping exited; restart prohibited')
        time.sleep(2)
    if process:
        process.terminate()


if __name__ == '__main__':
    main()
