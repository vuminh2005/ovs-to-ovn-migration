#!/usr/bin/env python3
"""Root guest service: structured serial evidence, no tenant SSH dependency."""
import concurrent.futures
import fnmatch
import json
import pathlib
import re
import subprocess
import socket
import shlex
import threading
import time
import urllib.request

BOOT = None
PING_SEQ = 0
PING_PROCESS = None
DHCP_THREAD = None
CONFIG = {}
DHCP = {'ack_count': 0, 't1_seconds': None, 't2_seconds': None, 'last_ack_monotonic': None,
        'last_renewal_interval_seconds': None}
DHCP_LOCK = threading.Lock()

def config_files(root, directories, pattern):
    """Same basename is shadowed by later directories; names sort globally."""
    selected = {}
    for directory in directories:
        for path in (root/directory).glob(pattern):
            selected[path.name] = path
    return [selected[name] for name in sorted(selected)]


def networkd_entries(text):
    """Read sectioned settings in order, including repeated sections/resets."""
    section = ''
    for line in text.replace('\\\n', '').splitlines():
        line = line.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
        elif '=' in line:
            key, value = line.split('=', 1)
            yield section, key.strip(), value.strip()


def config_boolean(value):
    if type(value) is bool:
        return value
    if isinstance(value, str):
        if value.lower() in ('yes', 'true', 'on', '1'):
            return True
        if value.lower() in ('no', 'false', 'off', '0'):
            return False
    return None


def config_mtu(value):
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and value.isdecimal() and int(value) > 0:
        return int(value)
    return None


def netplan_settings(root, interface):
    files = config_files(root, ('lib/netplan', 'etc/netplan', 'run/netplan'), '*.yaml')
    if not files:
        return {}
    # Optional: Ubuntu cloud-init normally supplies PyYAML. Its absence must
    # not interrupt health evidence or prevent reading generated networkd INI.
    try:
        import yaml
    except ImportError:
        return {'note': 'netplan_parser_unavailable'}
    def merge(old, new):
        for key, value in new.items():
            if isinstance(value, dict) and isinstance(old.get(key), dict):
                merge(old[key], value)
            elif isinstance(value, list) and isinstance(old.get(key), list):
                old[key] += value
            else:
                old[key] = value
    config = {}
    try:
        for path in files:
            data = yaml.safe_load(path.read_text()) or {}
            if not isinstance(data, dict):
                return {'note': 'netplan_parse_unknown'}
            merge(config, data)
    except (OSError, ValueError, yaml.YAMLError):
        return {'note': 'netplan_parse_unknown'}
    network = config.get('network', {})
    matches = []
    for kind in ('ethernets', 'wifis', 'bridges', 'bonds', 'vlans'):
        for name, settings in network.get(kind, {}).items():
            match = settings.get('match')
            if match is None:
                matched = name == interface.get('ifname')
            else:
                # Unsupported criteria must never attribute another NIC's MTU.
                matched = bool(match) and not (set(match)-{'name', 'macaddress'})
                if 'name' in match:
                    matched = matched and fnmatch.fnmatchcase(interface.get('ifname',''), match['name'])
                if 'macaddress' in match:
                    matched = matched and interface.get('address','').lower() == match['macaddress'].lower()
                if settings.get('set-name') == interface.get('ifname') and set(match) == {'name'}:
                    matched = True
            if matched:
                matches.append(settings)
    if len(matches) != 1:
        return {'note': 'netplan_interface_unknown'}
    settings = matches[0]
    return {'renderer': settings.get('renderer', network.get('renderer')),
            'mtu': config_mtu(settings.get('mtu')),
            'use_mtu': config_boolean(settings.get('dhcp4-overrides', {}).get('use-mtu'))}


def networkd_match(entries, interface):
    matches = {}
    for section, key, value in entries:
        if section == 'Match':
            matches.setdefault(key, [])
            if not value:
                matches[key] = []
            else:
                matches[key] += shlex.split(value)
    if set(matches)-{'Name', 'MACAddress'}:
        return None
    for key, patterns in matches.items():
        if not patterns:
            continue
        actual = interface.get('ifname','') if key == 'Name' else interface.get('address','').lower()
        inverted = patterns[0].startswith('!')
        if inverted:
            patterns = [patterns[0][1:]] + patterns[1:]
        matched = any(fnmatch.fnmatchcase(actual, p if key == 'Name' else p.lower()) for p in patterns)
        if matched == inverted:
            return False
    return True


def network_diagnostics(interface, root=pathlib.Path('/')):
    """Read-only, best-effort MTU configuration evidence for the IP-bearing NIC."""
    result = dict(network_backend='unknown', configured_static_mtu=None,
                  dhcp_use_mtu=None, mtu_configuration='unknown')
    if not interface or not interface.get('ifname'):
        return result
    try:
        netplan = netplan_settings(root, interface)
        if netplan.get('note'):
            result['network_config_note'] = netplan['note']
        if netplan.get('renderer') in ('networkd', 'NetworkManager'):
            result['network_backend'] = netplan['renderer']
        result['configured_static_mtu'] = netplan.get('mtu')
        result['dhcp_use_mtu'] = netplan.get('use_mtu')
        directories = ('usr/lib/systemd/network', 'usr/local/lib/systemd/network',
                       'run/systemd/network', 'etc/systemd/network')
        selected = None
        # Prefer the file actually selected for this link over guessing matches.
        if type(interface.get('ifindex')) is int:
            state = root/'run/systemd/netif/links'/str(interface['ifindex'])
            if state.is_file():
                active = dict((k,v) for _,k,v in networkd_entries(state.read_text())).get('NETWORK_FILE')
                if active:
                    selected = root/active.lstrip('/')
                    result['network_config_selection'] = 'networkd_active'
        if selected is None and result['network_backend'] != 'NetworkManager':
            for path in config_files(root, directories, '*.network'):
                if not path.read_text().strip():  # masked or empty
                    continue
                dropins = config_files(root, tuple(d+'/'+path.name+'.d' for d in directories), '*.conf')
                entries = list(networkd_entries('\n'.join(p.read_text() for p in [path]+dropins)))
                matched = networkd_match(entries, interface)
                if matched is None:
                    result['network_config_note'] = 'networkd_match_unknown'
                    break
                if matched:
                    selected = path
                    result['network_config_selection'] = 'networkd_match'
                    break
        if selected is not None:
            result['network_backend'] = 'networkd'
            result['network_config_file'] = '/'+str(selected.relative_to(root))
            dropins = config_files(root, tuple(d+'/'+selected.name+'.d' for d in directories), '*.conf')
            # Generated networkd settings take precedence over netplan intent.
            result['dhcp_use_mtu'] = None
            anonymize = False
            for section, key, value in networkd_entries('\n'.join(p.read_text() for p in [selected]+dropins)):
                if section == 'Link' and key == 'MTUBytes':
                    result['configured_static_mtu'] = config_mtu(value)
                elif section in ('DHCP', 'DHCPv4') and key == 'UseMTU':
                    result['dhcp_use_mtu'] = config_boolean(value)
                elif section in ('DHCP', 'DHCPv4') and key == 'Anonymize':
                    anonymize = config_boolean(value)
            if anonymize is True:
                result['dhcp_use_mtu'] = False
        if result['configured_static_mtu'] is not None:
            result['mtu_configuration'] = 'static_mtu'
        elif result['dhcp_use_mtu'] is False:
            result['mtu_configuration'] = 'dhcp_mtu_disabled'
        elif result['dhcp_use_mtu'] is True:
            result['mtu_configuration'] = 'dhcp_mtu_enabled'
    except Exception as exc:
        # Diagnostics must never suppress existing DHCP/packet/metadata evidence.
        result = dict(network_backend='unknown', configured_static_mtu=None, dhcp_use_mtu=None,
                      mtu_configuration='unknown', network_config_note=type(exc).__name__)
    return result


def emit(record):
    record.update(run=CONFIG['run'], vm=CONFIG['vm'], boot=BOOT)
    with open('/dev/ttyS0', 'w') as console:
        console.write('OVN_MIGRATION_JSON ' + json.dumps(record, separators=(',', ':')) + '\n')

def packet(seq, ts, mono):
    try:
        ok = subprocess.run(['ping', '-n', '-c', '1', '-W', '1', '-s', '56', CONFIG['peer']],
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
                            previous = DHCP['last_ack_monotonic']
                            DHCP['last_renewal_interval_seconds'] = received-previous if previous is not None else None
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
              dhcp_ack_count=observed['ack_count'], dhcp_last_ack_monotonic=observed['last_ack_monotonic'],
              dhcp_last_renewal_interval_seconds=observed['last_renewal_interval_seconds'],
              dhcp_t1_seconds=observed['t1_seconds'], dhcp_t2_seconds=observed['t2_seconds'],
              dhcp_observer_running=bool(DHCP_THREAD and DHCP_THREAD.is_alive()),
              continuous_ping_running=bool(PING_PROCESS and PING_PROCESS.poll() is None),
              **network_diagnostics(interfaces[0] if interfaces else None)))

def continuous_ping():
    """One lifetime iputils session for Pair A; stdout is secondary evidence only."""
    global PING_PROCESS, PING_SEQ
    PING_PROCESS = subprocess.Popen(['ping', '-n', '-D', '-O', '-s', '56', '-i', str(CONFIG['interval']), CONFIG['peer']],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in PING_PROCESS.stdout:
        match = re.search(r'icmp_seq[= ](\d+)', line)
        if not match:
            continue
        seq = int(match[1])
        logical=(PING_SEQ//65536)*65536+seq
        if logical<PING_SEQ-32768: logical+=65536
        if logical>PING_SEQ+32768: logical-=65536
        if logical<=0: continue
        PING_SEQ = max(PING_SEQ, logical)
        success = 'bytes from' in line
        emit(dict(kind='packet',seq=logical,ts=time.time(),mono=time.monotonic(),success=success,
                  diagnostic_only=True))
    emit(dict(kind='error',ts=time.time(),error='Continuous ping exited; capture metric must fail coverage'))


if __name__ == '__main__':
    BOOT = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    CONFIG = json.loads(pathlib.Path('/etc/migration-probe.json').read_text())
    DHCP_THREAD = threading.Thread(target=observe_dhcp, daemon=True)
    DHCP_THREAD.start()
    deadline = time.monotonic() + CONFIG['lifetime']
    continuous = CONFIG.get('continuous_ping') is True
    if continuous:
        threading.Thread(target=continuous_ping, daemon=True).start()
    seq = 0
    next_health = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        while time.monotonic() < deadline:
            started = time.monotonic()
            seq += 1
            if not continuous:
                pool.submit(packet, seq, time.time(), started)
            if started >= next_health:
                pool.submit(health, PING_SEQ if continuous else seq)
                next_health = started + 5
            time.sleep(max(0, CONFIG['interval'] - (time.monotonic() - started)))
    if PING_PROCESS and PING_PROCESS.poll() is None:
        PING_PROCESS.terminate()
    emit(dict(kind='expired', ts=time.time()))
