#!/usr/bin/env python3
"""Compute-local, API-independent capture and strict classic-PCAP ICMP accounting."""
import argparse
import fcntl
import json
import os
import pathlib
import re
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET


def save(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def identity(pid):
    try:
        # comm can contain spaces or parentheses; starttime is field 22.
        fields = pathlib.Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return {'pid': pid, 'start_ticks': fields[19],
                'boot': pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except (OSError, IndexError):
        return None


def alive(saved):
    return bool(saved and identity(saved.get('pid')) == saved)


def safe_interface_name(name):
    return isinstance(name, str) and name not in ('.', '..') and bool(re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', name))


def tap_from_xml(text, server, port, require_port=True):
    """Cross-check positive full-UUID XML evidence; absence can be optional."""
    domain = ET.fromstring(text)
    if domain.findtext('uuid') != server:
        raise RuntimeError('Libvirt domain UUID does not match checkpointed measure0')
    nics = [nic for nic in domain.findall('./devices/interface')
            if any(p.get('interfaceid') == port for p in nic.findall('.//virtualport/parameters'))]
    if not nics and not require_port:
        return None
    targets = [nic.find('target') for nic in nics]
    if not require_port and len(targets) == 1 and (targets[0] is None or not targets[0].get('dev')):
        return None  # no positive tap target to cross-check
    if len(targets) != 1 or targets[0] is None or not safe_interface_name(targets[0].get('dev')):
        raise RuntimeError('Exact checkpointed Neutron port must identify one libvirt tap')
    return targets[0].get('dev')


def tap_from_ovsdb(port, server, saved_tap=None, integration_bridge='br-int'):
    """Select one full iface-id, then verify ownership, presence and bridge."""
    raw = json.loads(subprocess.check_output([
        'ovs-vsctl', '--timeout=10', '--format=json', '--columns=name,external_ids',
        'find', 'Interface', 'external_ids:iface-id='+json.dumps(port)], text=True))
    try:
        if len(raw['data']) != 1:
            raise RuntimeError('Exact checkpointed Neutron port must match exactly one OVS Interface')
        row = dict(zip(raw['headings'], raw['data'][0]))
        encoded = row['external_ids']
        if (len(encoded) != 2 or encoded[0] != 'map' or
                any(not isinstance(k,str) or not isinstance(v,str) for k,v in encoded[1])):
            raise ValueError('Invalid OVS external_ids map')
        external = dict(encoded[1])
        if len(external) != len(encoded[1]):
            raise ValueError('Duplicate OVS external_ids keys')
        tap = row['name']
        if not safe_interface_name(tap):
            raise RuntimeError('OVS Interface has an unsafe/empty Linux interface name')
        if external.get('iface-id') != port or external.get('vm-uuid') != server:
            raise RuntimeError('OVS Interface full port/server UUID identity conflicts with checkpoint')
        if 'iface-status' in external and external['iface-status'] != 'active':
            raise RuntimeError('Checkpointed OVS Interface is not active')
        if saved_tap is not None and tap != saved_tap:
            raise RuntimeError('Saved capture tap conflicts with current exact OVS Interface; replacement prohibited')
    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeError('Invalid structured OVS Interface identity evidence') from exc
    if not pathlib.Path('/sys/class/net', tap).exists():
        raise RuntimeError('Checkpointed measure0 tap does not exist on selected compute')
    # iface-to-br verifies current membership, including the Port->Interface
    # relation. A missing/detached Interface is an error, never a name guess.
    bridge = subprocess.check_output(['ovs-vsctl', '--timeout=10', 'iface-to-br', tap], text=True).strip()
    if not safe_interface_name(integration_bridge) or bridge != integration_bridge:
        raise RuntimeError('Checkpointed tap is not on the expected integration bridge '+str(integration_bridge))
    return tap


def verify_saved_tap(cfg, tap):
    if not safe_interface_name(tap):
        raise RuntimeError('Missing/unsafe saved capture tap; replacement prohibited')
    return tap_from_ovsdb(cfg['port'], cfg['server'], saved_tap=tap,
                          integration_bridge=cfg.get('integration_bridge', 'br-int'))


def frames(path, live=False):
    """Classic PCAP, Ethernet only; truncated final records allowed only while live."""
    with path.open('rb') as f:
        header = f.read(24)
        formats = {b'\xd4\xc3\xb2\xa1':('<',1e6), b'\xa1\xb2\xc3\xd4':('>',1e6),
                   b'\x4d\x3c\xb2\xa1':('<',1e9), b'\xa1\xb2\x3c\x4d':('>',1e9)}
        if len(header) != 24 or header[:4] not in formats:
            raise ValueError('Missing/invalid classic PCAP header')
        endian, scale = formats[header[:4]]
        major, minor, _, _, _, link = struct.unpack(endian+'HHIIII', header[4:])
        if (major,minor,link) != (2,4,1):
            raise ValueError('Capture must use Ethernet classic PCAP')
        while True:
            chunk = f.read(16)
            if not chunk:
                break
            if len(chunk) != 16:
                if live: break
                raise ValueError('Truncated PCAP record header')
            sec, frac, size, original = struct.unpack(endian+'IIII', chunk)
            if size > 65535 or frac >= scale:
                raise ValueError('Invalid PCAP record')
            frame = f.read(size)
            if len(frame) != size:
                if live: break
                raise ValueError('Truncated PCAP frame')
            if size != original:
                raise ValueError('Snap length truncated a captured frame')
            yield sec+frac/scale, frame


def echo_rows(path, source, peer, live=False):
    requests = []
    pending = {}
    last = None
    problems = []
    for ts, frame in frames(path, live):
        offset = 14
        if len(frame) < offset: continue
        ether = frame[12:14]
        while ether in (b'\x81\x00', b'\x88\xa8'):
            if len(frame) < offset+4: raise ValueError('Truncated VLAN header')
            ether = frame[offset+2:offset+4]; offset += 4
        if ether != b'\x08\x00': continue
        ip = frame[offset:]
        if len(ip)<20 or ip[0]>>4 != 4 or ip[9] != 1: continue
        ihl = (ip[0]&15)*4
        length = int.from_bytes(ip[2:4],'big')
        if len(ip)<length or length<ihl+8 or ihl<20:
            raise ValueError('Malformed IPv4/ICMP frame')
        src, dst = socket.inet_ntoa(ip[12:16]), socket.inet_ntoa(ip[16:20])
        body = ip[ihl:length]
        if body[1] != 0 or body[0] not in (0,8): continue
        ident, seq = struct.unpack('!HH',body[4:8])
        token = (ident,seq,body[8:])  # payload disambiguates sequence rollover
        if body[0] == 8 and src == source and dst == peer:
            row = dict(index=len(requests)+1,ts=ts,identifier=ident,icmp_sequence=seq,reply_timestamp=None)
            if len(body[8:]) != 56:
                problems.append('Measurement payload is not 56 bytes')
            if last and (ident != last['identifier'] or seq != (last['icmp_sequence']+1)%65536):
                problems.append('ICMP identifier/sequence discontinuity')
            if token in pending:
                problems.append('Duplicate request identity/payload')
            requests.append(row); pending[token] = row; last = row
        elif body[0] == 0 and src == peer and dst == source:
            row = pending.get(token)
            if row is not None and row['reply_timestamp'] is None:
                if ts < row['ts']: problems.append('Reply predates request')
                row['reply_timestamp'] = ts
    return requests, problems


def recovered_endpoint(rows, interval):
    """Five latest captured requests replied; no human ping output involved."""
    tail = rows[-5:]
    if len(tail)!=5 or not all(r['reply_timestamp'] is not None for r in tail): return None
    if not all(0 < b['ts']-a['ts'] < 3*interval for a,b in zip(tail,tail[1:])): return None
    return {'index':tail[-1]['index'],'timestamp':tail[-1]['ts'],
            'reply_timestamp':tail[-1]['reply_timestamp'],'identifier':tail[-1]['identifier'],
            'icmp_sequence':tail[-1]['icmp_sequence']}


def pcap_metrics(path, checkpoint, window, interval):
    result = dict(status='UNAVAILABLE',evidence_source='compute-tap-pcap',measurement_workload='Pair A',
        measurement_guest='measure0',measurement='small-packet routed tenant dataplane',
        packets_attempted=None,packets_successful=None,packets_failed=None,packet_loss_percent=None,
        failure_burst_count=None,maximum_consecutive_failed_probes=None,first_failure_timestamp=None,
        recovery_timestamp=None,longest_outage_start_timestamp=None,longest_outage_recovery_timestamp=None,
        actual_dataplane_outage_seconds=None,coverage_complete=False,start_anchor=window.get('pcap_start'),end_anchor=window.get('pcap_end'))
    try:
        rows, problems = echo_rows(path,checkpoint['source_ip'],checkpoint['peer_ip'])
        start,end = window.get('pcap_start'),window.get('pcap_end')
        if not start or not end or end['index']<=start['index']:
            raise ValueError('Missing valid PCAP measurement endpoints')
        for anchor in (start,end):
            if type(anchor['index']) is not int or anchor['index']<1:
                raise ValueError('Invalid PCAP endpoint index')
            row=rows[anchor['index']-1]
            if any(anchor[k]!=row[k] for k in ('identifier','icmp_sequence')) or anchor['timestamp']!=row['ts'] or row['reply_timestamp'] is None or anchor['reply_timestamp']!=row['reply_timestamp']:
                raise ValueError('Measurement endpoint not present in raw PCAP')
        selected=rows[start['index']:end['index']]
        timed=rows[start['index']-1:end['index']]
        if len(selected)!=end['index']-start['index']:
            problems.append('Incomplete request coverage')
        if not all(0 < b['ts']-a['ts'] < 3*interval for a,b in zip(timed,timed[1:])):
            problems.append('Unexplained request cadence/capture gap')
        remote=checkpoint.get('remote',{})
        if (remote.get('status')!='STOPPED' or remote.get('returncode')!=0 or remote.get('dropped_packets')!=0 or
            remote.get('started_at',float('inf'))>start['timestamp'] or
            remote.get('stopped_at',0)<end['reply_timestamp'] or remote.get('supervisor_gap',True)):
            problems.append('Continuous capture lifetime/drop-free completion not proven')
        bursts=[]; burst=None
        for r in selected:
            if r['reply_timestamp'] is None:
                if burst is None: burst={'start_timestamp':r['ts'],'failed_probes':0,'recovery_timestamp':None,'duration_seconds':None}
                burst['failed_probes']+=1
            elif burst:
                burst.update(recovery_timestamp=r['reply_timestamp'],duration_seconds=r['reply_timestamp']-burst['start_timestamp'])
                bursts.append(burst); burst=None
        if burst: bursts.append(burst); problems.append('Unrecovered final loss burst')
        failed=sum(r['reply_timestamp'] is None for r in selected)
        result.update(packets_attempted=len(selected),packets_successful=len(selected)-failed,packets_failed=failed,
            failure_burst_count=len(bursts),maximum_consecutive_failed_probes=max((b['failed_probes'] for b in bursts),default=0),
            first_failure_timestamp=bursts[0]['start_timestamp'] if bursts else None,
            recovery_timestamp=bursts[0]['recovery_timestamp'] if bursts else None,failure_bursts=bursts)
        if problems: raise ValueError('; '.join(sorted(set(problems))))
        longest=max(bursts,key=lambda b:b['duration_seconds']) if bursts else None
        result.update(status='PASS',coverage_complete=True,packet_loss_percent=100*failed/len(selected),
            actual_dataplane_outage_seconds=longest['duration_seconds'] if longest else 0.0,
            longest_outage_start_timestamp=longest['start_timestamp'] if longest else None,
            longest_outage_recovery_timestamp=longest['recovery_timestamp'] if longest else None)
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        result['reason']=str(exc)
    return result


def resolve_tap(cfg):
    tap = tap_from_ovsdb(cfg['port'], cfg['server'], saved_tap=cfg.get('tap'),
                         integration_bridge=cfg.get('integration_bridge', 'br-int'))
    # Kolla exposes virsh inside nova_libvirt, not on the compute host.
    xml = subprocess.check_output(['docker','exec','nova_libvirt','virsh','dumpxml',cfg['server']], text=True)
    xml_tap = tap_from_xml(xml, cfg['server'], cfg['port'], require_port=False)
    if xml_tap is not None and xml_tap != tap:
        raise RuntimeError('Exact port libvirt tap conflicts with OVSDB tap identity')
    return tap


def _agent_start(root, cfg):
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=root/'capture-state.json'
    if path.exists():
        state=json.loads(path.read_text())
        if any(state['config'].get(k)!=cfg.get(k) for k in ('server','port','source_ip','peer_ip','run')):
            raise RuntimeError('Capture ownership/configuration changed')
        if state.get('status')=='RUNNING' and alive(state.get('supervisor')) and alive(state.get('tcpdump')):
            if cfg.get('tap') is not None and cfg['tap'] != state.get('tap'):
                raise RuntimeError('Controller and compute saved tap conflict; replacement prohibited')
            verify_saved_tap(state['config'], state.get('tap'))
            return state
        raise RuntimeError('Capture intent already exists but process state is ambiguous/stopped; refusing duplicate capture')
    if cfg.get('allow_create') is False:
        raise RuntimeError('Controller capture intent exists but compute journal is absent; refusing ambiguous replacement')
    if (root/'measure0.pcap').exists() or (root/'stop-request').exists():
        raise RuntimeError('Orphaned capture evidence exists without journal; refusing overwrite')
    if any(p.name not in ('dataplane_capture.py','start.lock') for p in root.iterdir()):
        raise RuntimeError('Capture directory contains non-owned files; refusing capture/cleanup ownership')
    tap=resolve_tap(cfg)
    state=dict(status='START_INTENT',config=cfg,tap=tap,path=str(root/'measure0.pcap'),intent_at=time.time())
    save(path,state)  # before launch; ambiguous launch cannot erase/replace evidence
    with (root/'supervisor.log').open('ab') as log:
        subprocess.Popen([sys.executable,str(pathlib.Path(__file__).resolve()),'supervise',str(root)],
                         stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        state=json.loads(path.read_text())
        if state['status']=='RUNNING': return state
        if state['status']=='FAILED': raise RuntimeError(state.get('reason','capture failed'))
        time.sleep(.1)
    raise TimeoutError('Capture launch uncertain; intent retained; do not launch duplicate')


def agent_start(root, cfg):
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (root/'start.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        return _agent_start(root,cfg)


def supervise(root):
    path=root/'capture-state.json'; state=json.loads(path.read_text()); cfg=state['config']
    state['supervisor']=identity(os.getpid()); state['supervisor_gap']=False
    try:
        verify_saved_tap(cfg, state['tap'])
        with (root/'tcpdump.log').open('wb') as log:
            child=subprocess.Popen(['tcpdump','-U','-n','-s','0','-i',state['tap'],'-w',state['path'],
                                    f"icmp and host {cfg['source_ip']} and host {cfg['peer_ip']}"],
                                   stdin=subprocess.DEVNULL,stdout=log,stderr=log)
            state.update(tcpdump=identity(child.pid),started_at=time.time(),status='RUNNING')
            save(path,state)
            previous=time.monotonic(); stopping=False
            while child.poll() is None:
                now=time.monotonic()
                state['supervisor_gap'] |= now-previous>3
                previous=now
                if (root/'stop-request').exists() and not stopping:
                    child.send_signal(signal.SIGINT); stopping=True
                state['heartbeat_at']=time.time(); save(path,state); time.sleep(.25)
            state.update(stopped_at=time.time(),returncode=child.returncode,status='STOPPED' if stopping and child.returncode==0 else 'FAILED')
            if state['status']=='FAILED':
                reason=f'tcpdump exited unexpectedly with return code {child.returncode}'
                if child.returncode < 0:
                    try:
                        reason+=f' ({signal.Signals(-child.returncode).name})'
                    except ValueError:
                        reason+=f' (signal {-child.returncode})'
                state['reason']=reason
        match=re.search(r'(\d+) packets dropped by kernel', (root/'tcpdump.log').read_text())
        state['dropped_packets']=int(match[1]) if match else None
        save(path,state)
    except Exception as exc:
        state.update(status='FAILED',reason=str(exc)); save(path,state)
        raise


def agent_snapshot(root, stop=False):
    path=root/'capture-state.json'; state=json.loads(path.read_text())
    if state['status']=='RUNNING':
        if not alive(state.get('supervisor')) or not alive(state.get('tcpdump')):
            raise RuntimeError('Exact capture process lost; evidence preserved, replacement prohibited')
        verify_saved_tap(state['config'], state.get('tap'))
        if stop:
            (root/'stop-request').touch(mode=0o600)
            deadline=time.monotonic()+15
            while state['status']=='RUNNING' and time.monotonic()<deadline:
                time.sleep(.1); state=json.loads(path.read_text())
            if state['status']!='STOPPED': raise RuntimeError('Capture did not stop cleanly')
    elif state['status']!='STOPPED':
        raise RuntimeError('Capture state ambiguous/failed; evidence preserved: '+state.get('reason','')+'; '+((root/'tcpdump.log').read_text()[-1000:] if (root/'tcpdump.log').exists() else ''))
    try:
        rows,problems=echo_rows(pathlib.Path(state['path']),state['config']['source_ip'],state['config']['peer_ip'],live=state['status']=='RUNNING')
        # An outstanding newest request is normal; recovery must be recent,
        # not a successful tail from an expired/stopped guest ping.
        settled=[r for r in rows if time.time()-r['ts']>=max(1,3*state['config']['interval'])]
        endpoint=recovered_endpoint(settled,state['config']['interval'])
        if endpoint and state.get('heartbeat_at',time.time())-endpoint['timestamp']>max(2,6*state['config']['interval']):
            endpoint=None
        state['observed_endpoint']=endpoint; state['parse_problems']=problems
        state['last_request_index']=rows[-1]['index'] if rows else 0
    except (ValueError,OSError):
        state['observed_endpoint']=None
    return state


def inventory_compute_host(inventory, binding_host):
    hosts=inventory.get('_meta',{}).get('hostvars',{})
    candidates=set(); visited=set()
    def visit(group):
        if group in visited: return
        visited.add(group); data=inventory.get(group,{})
        candidates.update(data.get('hosts',[]))
        for child in data.get('children',[]): visit(child)
    visit('compute')
    matches=[h for h in candidates if binding_host in
             (h,hosts.get(h,{}).get('ansible_host'),hosts.get(h,{}).get('ansible_hostname'))]
    if len(matches)!=1:
        raise RuntimeError('Neutron binding host must resolve to exactly one compute inventory host; check aliases/ansible_hostname')
    return matches[0]


class Capture:
    """Controller transport via the existing Ansible inventory, no cloud API after start."""
    def __init__(self, validation):
        self.v=validation; self.path=validation.root/'pair-a-capture.json'

    def checkpoint(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def transport(self, action, checkpoint):
        extra=self.v.root/'capture-transport.json'
        save(extra,dict(capture_action=action,capture_checkpoint=checkpoint,capture_root=str(self.v.root)))
        helper=pathlib.Path(__file__).parent.parent/'playbooks/dataplane-capture-tasks.yml'
        completed=subprocess.run(['ansible-playbook','-i',self.v.cfg['inventory'],str(helper),'-e','@'+str(extra)],
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=self.v.cfg.get('timeout',300))
        log=self.v.root/('capture-transport-'+action+'.log')
        log.write_text(completed.stdout+'\n'+completed.stderr); log.chmod(0o600)
        if completed.returncode:
            raise RuntimeError('Compute capture '+action+' failed; see '+str(log)+': '+(completed.stdout+completed.stderr)[-2000:])
        if action=='remove': return {}
        return json.loads((self.v.root/'capture-remote-state.json').read_text())

    def start(self):
        checkpoint=self.checkpoint()
        first_launch=not checkpoint
        if not checkpoint:
            pair=self.v.pair('measure'); vm=pair['0']
            if any(v.get('owned') is not True for v in pair.values()):
                raise RuntimeError('Capture requires checkpointed owned Pair A')
            server=self.v.cloud.compute.get_server(vm['server']); port=self.v.cloud.network.get_port(vm['port'])
            if (server.id!=vm['server'] or port.id!=vm['port'] or port.device_id!=vm['server'] or port.fixed_ips!=vm['fixed_ips'] or
                server.status!='ACTIVE' or server.metadata.get('ovn_migration_run')!=self.v.cfg['run'] or
                server.metadata.get('ovn_validation_role')!='measure'):
                raise RuntimeError('Pair-A capture identity changed')
            host=port.binding_host_id
            if self.v.cfg.get('placement_enabled'):
                if self.v.placement(vm, server, port)['status'] != 'PASS':
                    raise RuntimeError('Pair A capture compute differs from placement checkpoint')
                compute_host=vm['placement']['inventory_host']
            else:
                inventory=json.loads(subprocess.check_output(['ansible-inventory','-i',self.v.cfg['inventory'],'--list'],text=True))
                compute_host=inventory_compute_host(inventory,host)
            run=str(uuid.UUID(self.v.cfg['run'])) if re.fullmatch(r'[0-9a-fA-F-]{36}',self.v.cfg['run']) else self.v.cfg['run']
            if run in ('.','..') or not re.fullmatch(r'[A-Za-z0-9_.-]+',run): raise ValueError('Unsafe capture run identifier')
            directory=str(pathlib.Path(self.v.cfg['capture_directory'])/run)
            checkpoint=dict(schema_version=1,status='START_INTENT',compute_host=compute_host,binding_host=host,
                server=vm['server'],port=vm['port'],source_ip=vm['ip'],peer_ip=pair['1']['ip'],run=run,
                interval=self.v.cfg['interval'],directory=directory,path=directory+'/measure0.pcap',intent_at=time.time())
            save(self.path,checkpoint)
        if first_launch:
            resolved=self.transport('resolve',checkpoint)
            if not safe_interface_name(resolved.get('tap')):
                raise RuntimeError('Compute tap resolution did not return a safe exact interface')
            checkpoint.update(tap=resolved['tap'],integration_bridge=resolved['integration_bridge'])
            save(self.path,checkpoint)  # exact tap persisted on controller BEFORE any launch
        remote=self.transport('start',dict(checkpoint,allow_create=first_launch))
        if checkpoint.get('tap') is not None and remote.get('tap') != checkpoint['tap']:
            raise RuntimeError('Compute capture tap conflicts with saved controller tap; replacement prohibited')
        original=checkpoint.get('remote',{})
        if original and any(remote.get(k)!=original.get(k) for k in ('supervisor','tcpdump','started_at','tap','path')):
            raise RuntimeError('Resume capture process identity changed; replacement prohibited')
        checkpoint.update(status=remote['status'],tap=remote['tap'],remote=remote)
        save(self.path,checkpoint)
        return remote

    def snapshot(self, stop=False):
        checkpoint=self.checkpoint()
        if not checkpoint: raise RuntimeError('No owned Pair-A capture checkpoint')
        remote=self.transport('stop' if stop else 'snapshot',checkpoint)
        original=checkpoint.get('remote',{})
        if (any(remote.get(k)!=original.get(k) for k in ('supervisor','tcpdump','started_at','tap','path')) or
            any(remote.get('config',{}).get(k)!=checkpoint.get(k) for k in ('server','port','source_ip','peer_ip','run'))):
            raise RuntimeError('Exact capture process/endpoint identity changed; evidence preserved')
        checkpoint.update(status=remote['status'],remote=remote); save(self.path,checkpoint)
        return remote

    def anchor(self, end=False):
        path=self.v.root/'validation-window.json'
        window=json.loads(path.read_text()) if path.exists() else {}
        name='pcap_end' if end else 'pcap_start'
        if window.get(name): return window[name]
        if end and not window.get('pcap_start'): raise RuntimeError('Missing authoritative capture start')
        deadline=time.monotonic()+self.v.cfg['timeout']
        if end and 'pcap_end_fence' not in window:
            remote=self.snapshot()
            window['pcap_end_fence']=remote.get('last_request_index',0)
            save(path,window)  # recovery must occur after final validation, immutable on resume
        while True:
            remote=self.snapshot()
            endpoint=remote.get('observed_endpoint')
            if endpoint and not remote.get('parse_problems') and (not end or endpoint['index']>=max(window['pcap_start']['index']+1,window['pcap_end_fence']+5)):
                window.update(measurement_workload='Pair A',evidence_source='compute-tap-pcap')
                window[name]=endpoint; save(path,window); return endpoint
            if time.monotonic()>=deadline: raise TimeoutError('Pair-A capture/persistent ping fresh request/reply recovery not proven')
            time.sleep(1)

    def finish(self):
        anchor_error=None
        try:
            self.anchor(end=True)
        except (RuntimeError,TimeoutError) as exc:
            anchor_error=str(exc)
        self.snapshot(stop=True)  # transport fetches PCAP and tcpdump diagnostics
        cp=self.checkpoint(); window=json.loads((self.v.root/'validation-window.json').read_text())
        result=pcap_metrics(self.v.root/'measure0.pcap',cp,window,self.v.cfg['interval'])
        if anchor_error:
            result.update(status='UNAVAILABLE',packet_loss_percent=None,actual_dataplane_outage_seconds=None,reason=anchor_error)
        # Boot identity is independently validated, not reconstructed from PCAP.
        checks=json.loads((self.v.root/'measure-post-checks.json').read_text()) if (self.v.root/'measure-post-checks.json').exists() else {}
        boots=checks.get('guests',{})
        result['pair_a_boot_continuity']=('PASS' if set(boots)=={'0','1'} and all(r.get('boot_continuity')=='PASS' for r in boots.values()) else
                                       'FAIL' if any(r.get('boot_continuity')=='FAIL' for r in boots.values()) else 'UNAVAILABLE')
        # Boot diagnostics affect workload validity/cleanup independently. The
        # authoritative request session itself is checked for ICMP discontinuity.
        save(self.v.root/'tenant-dataplane-probe.json',result)
        return result


def main():
    p=argparse.ArgumentParser(); p.add_argument('action',choices=['resolve','start','supervise','snapshot','stop']); p.add_argument('root',type=pathlib.Path); p.add_argument('config',nargs='?')
    args=p.parse_args()
    if args.action=='supervise': supervise(args.root); return
    if args.action=='resolve':
        cfg=json.loads(args.config)
        out=dict(status='RESOLVED',tap=resolve_tap(cfg),integration_bridge=cfg.get('integration_bridge','br-int'))
    elif args.action=='start': out=agent_start(args.root,json.loads(args.config))
    else: out=agent_snapshot(args.root,args.action=='stop')
    print(json.dumps(out))

if __name__=='__main__': main()
