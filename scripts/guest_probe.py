#!/usr/bin/env python3
"""Root guest service: structured serial evidence, no tenant SSH dependency."""
import concurrent.futures
import json
import pathlib
import re
import subprocess
import time
import urllib.request

BOOT = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
CONFIG = json.loads(pathlib.Path('/etc/migration-probe.json').read_text())

def emit(record):
    record.update(run=CONFIG['run'], vm=CONFIG['vm'], boot=BOOT)
    with open('/dev/ttyS0', 'w') as console:
        console.write('OVN_MIGRATION_JSON ' + json.dumps(record, separators=(',', ':')) + '\n')

def packet(seq, ts, mono):
    try:
        ok = subprocess.run(['ping', '-n', '-c', '1', '-W', '1', CONFIG['peer']],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        emit(dict(kind='packet', seq=seq, ts=ts, mono=mono, completed=time.time(), success=ok))
    except Exception as exc:
        emit(dict(kind='error', ts=time.time(), error=str(exc)))

def health(seq):
    # Require actual DHCP lease evidence, not just an address assigned in Neutron.
    leases = list(pathlib.Path('/run/systemd/netif/leases').glob('*'))
    leases += list(pathlib.Path('/var/lib/dhcp').glob('*leases*'))
    lease = '\n'.join(p.read_text(errors='replace') for p in leases if p.is_file())
    addr = subprocess.check_output(['ip', '-j', '-4', 'addr'], text=True)
    routes = subprocess.check_output(['ip', '-j', '-4', 'route'], text=True)
    configured = any(a.get('local') == CONFIG['ip'] for interface in json.loads(addr) for a in interface.get('addr_info', []))
    leased = bool(re.search(r'(?<![0-9.])' + re.escape(CONFIG['ip']) + r'(?![0-9.])', lease))
    dhcp = configured and leased and bool(json.loads(routes))
    metadata = False
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        body = opener.open('http://169.254.169.254/openstack/latest/meta_data.json', timeout=2).read()
        metadata = json.loads(body).get('uuid') == CONFIG['server_id']
    except Exception:
        pass
    emit(dict(kind='health', seq=seq, ts=time.time(), mono=time.monotonic(), dhcp=dhcp, metadata=metadata,
              addresses=json.loads(addr), routes=json.loads(routes), lease_present=bool(lease)))

if __name__ == '__main__':
    deadline = time.monotonic() + CONFIG['lifetime']
    seq = 0
    next_health = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        while time.monotonic() < deadline:
            started = time.monotonic()
            seq += 1
            pool.submit(packet, seq, time.time(), started)
            if started >= next_health:
                pool.submit(health, seq)
                next_health = started + 5
            time.sleep(max(0, CONFIG['interval'] - (time.monotonic() - started)))
    emit(dict(kind='expired', ts=time.time()))
