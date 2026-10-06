#!/usr/bin/env python3
"""Root guest service: structured serial evidence, no tenant SSH dependency."""
import concurrent.futures
import json
import pathlib
import re
import subprocess
import socket
import threading
import time
import urllib.request

BOOT = None
CONFIG = {}
DHCP = {'ack_count': 0, 't1_seconds': None, 't2_seconds': None, 'last_ack_monotonic': None}
DHCP_LOCK = threading.Lock()

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

def observe_dhcp():
    """Passively inspect real DHCP ACKs; never force renewal or reboot."""
    try:
        with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003)) as stream:
            pending_renewal = None
            while True:
                frame = stream.recv(65535)
                if frame[12:14] != b'\x08\x00':
                    continue
                ip = frame[14:]
                if len(ip) < 28 or ip[9] != 17:
                    continue
                header = (ip[0] & 15)*4
                udp = ip[header:]
                if udp[:4] not in (b'\x00\x43\x00\x44', b'\x00\x44\x00\x43'):
                    continue
                body = udp[8:]
                if len(body) < 240 or body[236:240] != b'\x63\x82\x53\x63':
                    continue
                options = {}
                offset = 240
                while offset < len(body):
                    key = body[offset]; offset += 1
                    if key == 255: break
                    if key == 0: continue
                    if offset >= len(body): break
                    size = body[offset]; offset += 1
                    options[key] = body[offset:offset+size]; offset += size
                token = (body[4:8], body[28:44])
                if udp[:4] == b'\x00\x44\x00\x43':
                    if body[0] == 1 and options.get(53) == b'\x03' and socket.inet_ntoa(body[12:16]) == CONFIG['ip']:
                        pending_renewal = token
                    continue
                if body[0] == 2 and options.get(53) == b'\x05' and socket.inet_ntoa(body[16:20]) == CONFIG['ip'] and pending_renewal == token:
                    pending_renewal = None
                    with DHCP_LOCK:
                        received = time.monotonic()
                        # A duplicated ACK must not masquerade as lease renewal.
                        if DHCP['last_ack_monotonic'] is None or received-DHCP['last_ack_monotonic'] >= max(1, CONFIG.get('dhcp_t1',30)/2):
                            DHCP['ack_count'] += 1
                            DHCP['last_ack_monotonic'] = received
                        DHCP['t1_seconds'] = int.from_bytes(options[58], 'big') if len(options.get(58,b'')) == 4 else None
                        DHCP['t2_seconds'] = int.from_bytes(options[59], 'big') if len(options.get(59,b'')) == 4 else None
    except Exception as exc:
        emit(dict(kind='error', ts=time.time(), error='DHCP ACK observer: '+str(exc)))


def health(seq):
    # Require actual DHCP lease evidence, not just an address assigned in Neutron.
    leases = list(pathlib.Path('/run/systemd/netif/leases').glob('*'))
    leases += list(pathlib.Path('/var/lib/dhcp').glob('*leases*'))
    lease = '\n'.join(p.read_text(errors='replace') for p in leases if p.is_file())
    addr = subprocess.check_output(['ip', '-j', '-4', 'addr'], text=True)
    routes = subprocess.check_output(['ip', '-j', '-4', 'route'], text=True)
    configured = any(a.get('local') == CONFIG['ip'] for interface in json.loads(addr) for a in interface.get('addr_info', []))
    leased = bool(re.search(r'(?<![0-9.])' + re.escape(CONFIG['ip']) + r'(?![0-9.])', lease))
    interfaces = [i for i in json.loads(addr) if any(a.get('local') == CONFIG['ip'] for a in i.get('addr_info', []))]
    usable = bool(interfaces) and 'UP' in interfaces[0].get('flags', [])
    basic_route = any(r.get('dst') == 'default' and r.get('gateway') for r in json.loads(routes))
    dhcp = configured and leased and usable and basic_route
    try:
        route = json.loads(subprocess.check_output(['ip', '-j', '-4', 'route', 'get', '169.254.169.254'], text=True))
    except subprocess.CalledProcessError:
        # A failed metadata route lookup must not hide a usable DHCP lease.
        route = []
    with DHCP_LOCK:
        observed = dict(DHCP)
    metadata = False
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        body = opener.open('http://169.254.169.254/openstack/latest/meta_data.json', timeout=2).read()
        metadata = json.loads(body).get('uuid') == CONFIG['server_id']
    except Exception:
        pass
    emit(dict(kind='health', seq=seq, ts=time.time(), mono=time.monotonic(), dhcp=dhcp, metadata=metadata,
              addresses=json.loads(addr), routes=json.loads(routes), lease_present=bool(lease),
              mtu=interfaces[0].get('mtu') if interfaces else None,
              metadata_gateway=route[0].get('gateway') if route else None,
              dhcp_ack_count=observed['ack_count'], dhcp_t1_seconds=observed['t1_seconds'], dhcp_t2_seconds=observed['t2_seconds']))

if __name__ == '__main__':
    BOOT = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    CONFIG = json.loads(pathlib.Path('/etc/migration-probe.json').read_text())
    threading.Thread(target=observe_dhcp, daemon=True).start()
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
