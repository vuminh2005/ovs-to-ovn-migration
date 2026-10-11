#!/usr/bin/env python3
"""Owned Pair-D lifecycle and fresh, boot/session-fenced TCP summaries."""
import json
import math
import pathlib
import subprocess
import time


class PairD:
    def __init__(self, validation):
        self.v = validation

    @property
    def enabled(self):
        return self.v.cfg.get('pair_d_enabled', False)

    def read(self, name):
        path = self.v.root/name
        return json.loads(path.read_text()) if path.exists() else {}

    def save(self, name, value):
        from workload_validation import save
        save(self.v.root/name, value)

    def latest(self, rows):
        result = {}
        for key in ('0', '1'):
            vm = self.v.pair('tcp')[key]
            matches = [r for r in rows[key] if r.get('kind') == 'tcp' and
                       r.get('server_id') == vm['server'] and isinstance(r.get('boot'), str) and
                       type(r.get('mono')) in (int, float) and math.isfinite(r['mono'])]
            if matches:
                # Observation order fences boots; never compare clocks across guests.
                boot = matches[-1]['boot']
                result[key] = max((r for r in matches if r['boot'] == boot), key=lambda r:r['mono'])
        return result

    def identities(self):
        result = {}
        for key, vm in self.v.pair('tcp').items():
            server = self.v.cloud.compute.get_server(vm['server'])
            port = self.v.cloud.network.get_port(vm['port'])
            identity = (vm.get('owned') is True and server.id == vm['server'] and port.id == vm['port'] and
                        port.device_id == server.id and port.fixed_ips == vm['fixed_ips'] and
                        server.metadata.get('ovn_migration_run') == self.v.cfg['run'] and
                        server.metadata.get('ovn_validation_role') == 'tcp' and
                        server.status == 'ACTIVE' and port.status == 'ACTIVE' and
                        port.binding_vif_type not in ('unbound', 'binding_failed'))
            result[key] = dict(identity='PASS' if identity else 'FAIL',
                               placement=self.v.placement(vm, server, port))
        return result

    def wait(self, post=False, freeze=False):
        if not self.enabled:
            self.save('pair-d-tcp.json', {'status': 'DISABLED', 'enabled': False})
            return
        baseline = self.read('pair-d-baseline.json')
        trigger = self.read('pair-d-trigger.json')
        deadline = time.monotonic()+self.v.cfg['timeout']
        anchor = self.latest(self.v.collect('tcp', deadline=deadline))
        while True:
            current = self.latest(self.v.collect('tcp', deadline=deadline))
            identities = self.identities()
            fresh = (set(current) == {'0','1'} and all(
                k in anchor and current[k]['boot'] == anchor[k]['boot'] and
                current[k]['mono'] > anchor[k]['mono'] for k in ('0','1')))
            for key in current:
                if key not in anchor:
                    anchor[key] = current[key]
            identity_ok = all(r['identity'] == 'PASS' and r['placement']['status'] == 'PASS'
                              for r in identities.values())
            client = current.get('0', {})
            totals = client.get('total', {})
            session_ok = client.get('held_established') is True and client.get('held_broken') is False
            original_ok = (not baseline or (set(current) == {'0','1'} and all(
                current[k]['boot'] == baseline['boots'][k] and
                all(self.v.pair('tcp')[k].get(field) == baseline['resources'][k].get(field)
                    for field in ('server','port','fixed_ips','network','subnet')) for k in ('0','1')) and
                client.get('session_id') == baseline.get('session_id')))
            initial_ok = all(totals.get(k, {}).get('consecutive_successes', 0) >= 5 for k in ('held', 'new_old'))
            streams = client.get('streams', {})
            ready = fresh and identity_ok and session_ok and original_ok and initial_ok
            opened_during_freeze = None
            if not post:
                ready = (ready and client.get('armed') is False and
                         current.get('1', {}).get('listener_opened') is False)
            if post:
                from phase_schema import timestamp
                start = timestamp(self.v.root/'metrics/control_plane_downtime.start')
                end = timestamp(self.v.root/'metrics/control_plane_downtime.end')
                acknowledged = trigger.get('acknowledged_at')
                opened_during_freeze = (start is not None and end is not None and
                    type(acknowledged) in (int, float) and start <= acknowledged <= end)
                ack = trigger.get('ack', {})
                ready = (ready and baseline and trigger.get('status') == 'PASS' and
                         opened_during_freeze and
                         ack.get('session_id') == baseline.get('session_id') and
                         ack.get('armed_mono') == client.get('armed_mono') and client.get('armed') is True and
                         current.get('1', {}).get('listener_opened') is True and
                         all(streams.get(k, {}).get('attempts', 0) >= 5 and
                             streams.get(k, {}).get('consecutive_successes', 0) >= 5 and
                             streams[k].get('open_failure_since') is None
                             for k in ('held', 'new_old', 'new_listener')))
            statistics = streams if post else totals
            results = {}
            for key, value in statistics.items():
                attempts = value.get('attempts', 0)
                recovered = (attempts >= 5 and value.get('consecutive_successes', 0) >= 5 and
                             value.get('open_failure_since') is None)
                good = recovered and (session_ok and original_ok if key == 'held' else True)
                results[key] = dict(value, status='PASS' if good else 'FAIL',
                                    failed_probe_percent=100*value.get('failures', 0)/attempts if attempts else None)
            evidence = dict(status='PASS' if ready else 'IN_PROGRESS', enabled=True,
                            evidence_source='guest-tcp-cumulative-summary', identities=identities,
                            fresh=fresh, boot_continuity=bool(original_ok),
                            session_preserved=bool(session_ok and original_ok), snapshot=current,
                            streams=results, trigger=trigger if post else None,
                            listener_opened_during_freeze=opened_during_freeze,
                            timing_scope='Client ARM acknowledgement to latest fresh client summary; application echo/connect probes',
                            interval_seconds=self.v.cfg['tcp_interval'], timeout_seconds=self.v.cfg['tcp_timeout'])
            filename = 'pair-d-tcp.json' if post else ('pair-d-precutover.json' if freeze else 'pair-d-readiness.json')
            self.save(filename, evidence)
            if ready:
                if not post and not baseline:
                    self.save('pair-d-baseline.json', dict(boots={k:r['boot'] for k,r in current.items()},
                        session_id=client['session_id'], identities=identities,
                        resources={k:{f:vm[f] for f in ('server','port','fixed_ips','network','subnet')}
                                   for k,vm in self.v.pair('tcp').items()},
                        servers={k:vm['server'] for k,vm in self.v.pair('tcp').items()},
                        ports={k:vm['port'] for k,vm in self.v.pair('tcp').items()}))
                return
            if client.get('held_broken') is True or (baseline and current and not original_ok) or time.monotonic() >= deadline:
                evidence.update(status='FAIL' if client.get('held_broken') or not original_ok else 'UNAVAILABLE',
                                reason='TCP freshness, held session, identity, placement or post-migration recovery not proven')
                self.save(filename, evidence)
                raise RuntimeError('Pair D '+evidence['reason'])
            time.sleep(2)

    def transport(self, action):
        pair = self.v.pair('tcp')
        payload = dict(action=action, run=self.v.cfg['run'], router=self.v.state['pre']['router'],
                       ip=pair['0']['ip'], port=self.v.cfg['tcp_control_port'])
        ack_filename = 'pair-d-trigger-ack.json' if action == 'ARM' else 'pair-d-control-precheck.json'
        extra = self.v.root/'pair-d-transport.json'
        self.save(extra.name, dict(pair_d_trigger=payload, pair_d_network_host=self.v.cfg['pair_d_network_host'],
                                  pair_d_root=str(self.v.root), pair_d_ack_filename=ack_filename))
        if action == 'ARM':
            self.save('pair-d-trigger.json', dict(status='INTENT', payload=payload, intent_at=time.time()))
        helper = pathlib.Path(__file__).parent.parent/'playbooks/pair-d-trigger-tasks.yml'
        completed = subprocess.run(['ansible-playbook', '-i', self.v.cfg['inventory'], str(helper), '-e', '@'+str(extra)],
                                   capture_output=True, text=True, timeout=self.v.cfg['timeout'])
        log = self.v.root/('pair-d-trigger.log' if action == 'ARM' else 'pair-d-control-precheck.log')
        log.write_text(completed.stdout+'\n'+completed.stderr)
        log.chmod(0o600)
        if completed.returncode:
            raise RuntimeError('Pair D '+action+' failed; inspect '+str(log))
        return payload, self.read(ack_filename)

    def control_precheck(self):
        if not self.enabled:
            return
        _, ack = self.transport('CHECK')
        baseline = self.read('pair-d-baseline.json')
        if (ack.get('status') != 'PASS' or ack.get('run') != self.v.cfg['run'] or
                ack.get('ready') is not True or ack.get('armed') is not False or
                ack.get('session_id') != baseline.get('session_id')):
            raise RuntimeError('Pair D control namespace/session precheck failed before freeze')

    def arm(self):
        if not self.enabled:
            return
        # This operation does not query Neutron: its API is already frozen.
        if not (self.v.root/'metrics/control_plane_downtime.start').exists() or (self.v.root/'metrics/control_plane_downtime.end').exists():
            raise RuntimeError('Pair D can be armed only inside the Neutron freeze window')
        if self.read('pair-d-trigger.json').get('status') == 'PASS':
            return  # Never reset the guest epoch during a retry.
        payload, ack = self.transport('ARM')
        baseline = self.read('pair-d-baseline.json')
        if (ack.get('status') != 'PASS' or ack.get('run') != self.v.cfg['run'] or ack.get('armed') is not True or
                ack.get('session_id') != baseline.get('session_id')):
            raise RuntimeError('Pair D ARM acknowledged a different held session')
        self.save('pair-d-trigger.json', dict(status='PASS', payload=payload, ack=ack, acknowledged_at=time.time()))
