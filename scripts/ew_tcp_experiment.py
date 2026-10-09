"""Separate two-port TCP experiment; no API, SSH or application-result inference."""
import json
import math
from pathlib import Path
from ew_transport import verify_profile
from phase_schema import marker_name, schema_version, timestamp


def restoration_markers(root):
    """Controller clocks only; guest sequence fences select recovery records."""
    root=Path(root)
    names=('control_plane_downtime.start','db_migration.start','db_migration.end',
           marker_name(schema_version(root),8,'end'),'control_plane_downtime.end')
    values={name:timestamp(root/'metrics'/name) for name in names}
    times=list(values.values())
    if any(t is None for t in times) or not times[0]<times[1]<=times[2]<=times[3]<=times[4]:
        raise ValueError('Missing or out-of-order Neutron freeze/DB/takeover/restoration markers')
    return values


def target_ready(root, session, catalog):
    row=session['tcp_recovery']; markers=restoration_markers(root)
    when=row['established_at_epoch']
    if (row['status']!='PASS' or row['run_id']!=session['run_id'] or
        type(when) not in (int,float) or not math.isfinite(when) or
        when<markers['control_plane_downtime.end'] or row['controller_markers']!=markers or
        type(row['client_fence']) is not int or row['client_fence']<=0):
        raise ValueError('Missing valid post-OVN TCP recovery anchor')
    journal=json.loads((Path(root)/'network-mtu-plan.json').read_text())['networks']
    for name in ('ew-app','ew-client-b'):
        vm=catalog[name]; baseline=session['guests'][name]; guest=row['guests'][name]
        plans=[p for p in journal if p['network']==vm['network']]
        if len(plans)!=1 or plans[0]['target_mtu']!=baseline['target_mtu']:
            raise ValueError('Missing/ambiguous TCP target MTU journal')
        if (guest['status']!='PASS' or guest['server']!=vm['server'] or guest['port']!=vm['port'] or
            guest['fixed_ip']!=vm['ip'] or guest['actual_host']!=vm['actual_host'] or
            guest['network']!=vm['network'] or guest['network_type']!='geneve' or
            guest['network_mtu']!=plans[0]['target_mtu'] or guest['boot']!=baseline['boot']):
            raise ValueError('TCP post-OVN endpoint identity/network/MTU mismatch')
        verify_profile(guest['guest'],vm,baseline['boot'],plans[0]['target_mtu'],baseline['mac'])
    return row


def report(root, assess):
    root=Path(root)
    try:
        if not (root/'ew-measurement-config.json').exists(): return dict(status='NOT TESTED',enabled=False)
        cfg=json.loads((root/'ew-measurement-config.json').read_text())
        if not cfg.get('enabled') or not cfg.get('tcp_experiment_enabled'): return dict(status='NOT TESTED',enabled=False)
        sessions=json.loads((root/'ew-lifecycle.json').read_text())['sessions']
        if 'migration' not in sessions and 'baseline' in sessions: return dict(status='NOT TESTED',enabled=False)
        session=sessions['migration']
        catalog=json.loads((root/'ew-resources.json').read_text())['servers']
        recovery=target_ready(root,session,catalog)
        activation=session['tcp_activation']; ports=cfg['tcp_ports']; streams={}
        for actor in ('ew-app','ew-client-b'):
            directory=root/'ew/migration'/actor
            if assess(directory,'migration')['coverage']!='PASS': raise ValueError('Incomplete TCP runner evidence')
            actual=json.loads((directory/'config.json').read_text())
            if actual!=session['runners'][actor]: raise ValueError('TCP runner configuration differs from checkpoint')
            if actual['server']!=catalog[actor]['server'] or actual['port']!=catalog[actor]['port']:
                raise ValueError('TCP endpoint ownership mismatch')
            streams[actor]=[json.loads(l) for l in (directory/'events.jsonl').read_text().splitlines()]
        if (activation['status']!='PASS' or activation['server']!=catalog['ew-app']['server'] or
            activation['port']!=catalog['ew-app']['port']): raise ValueError('No verified post-freeze listener activation')
        verified=activation['verified_at_epoch']
        freeze=float((root/'metrics/control_plane_downtime.start').read_text())
        db_start=float((root/'metrics/db_migration.start').read_text())
        if not all(math.isfinite(t) for t in (verified,freeze,db_start)) or not freeze<=verified<db_start:
            raise ValueError('Listener activation missing or outside the required freeze/DB ordering')
        bound=[r for r in streams['ew-app'] if r['kind']=='tcp_listener_activated']
        for port in ports:
            rows=[r for r in bound if r.get('port')==port]
            if len(rows)!=1 or rows[0]['run_id']!=session['run_id'] or rows[0]['boot']!=session['guests']['ew-app']['boot']:
                raise ValueError('Missing/duplicate/incorrect listener bind identity')
        first=next(r for r in bound if r['port']==ports[0]); second=next(r for r in bound if r['port']==ports[1])
        if not first['seq']<=session['initial_coverage']['ew-app']<=activation['server_fence']<second['seq']:
            raise ValueError('Listener sequence does not prove pre-migration first and post-freeze second bind')
        if activation['binding']['seq']!=second['seq']: raise ValueError('Activation observation differs from raw bind')
        if (recovery['client_fence']<max(activation['client_fence'],session['initial_coverage']['ew-client-b']) or
            not any(r['seq']==recovery['client_fence'] for r in streams['ew-client-b'])):
            raise ValueError('Post-OVN TCP fence contradicts collected client sequence history')
        results={}
        for port in ports:
            all_rows=[r for r in streams['ew-client-b'] if r['kind']=='tcp_echo' and r.get('port')==port]
            if any(r.get('run_id')!=session['run_id'] or r.get('boot')!=session['guests']['ew-client-b']['boot'] for r in all_rows):
                raise ValueError('TCP client run/boot continuity mismatch')
            if any(type(r.get('ok')) is not bool for r in all_rows): raise ValueError('Malformed TCP echo result')
            fence=activation['client_fence'] if port==ports[1] else 0
            rows=[r for r in all_rows if r['seq']>fence]
            if len(rows)<cfg['stable_samples']: raise ValueError('Missing post-activation TCP connection evidence')
            limit=2*cfg['interval']+5  # two bounded connection/echo attempts per loop
            end=streams['ew-client-b'][-1]['mono']
            start=streams['ew-client-b'][0]['mono'] if not fence else next(r['mono'] for r in streams['ew-client-b'] if r['seq']==fence)
            if rows[0]['mono']-start>limit or end-rows[-1]['mono']>limit or any(b['mono']-a['mono']>limit for a,b in zip(rows,rows[1:])):
                raise ValueError('TCP attempt coverage gap')
            if port==ports[0]:
                initial=[r for r in rows if r['seq']<=session['initial_coverage']['ew-client-b']]
                if len(initial)<cfg['stable_samples'] or not all(r['ok'] for r in initial[-cfg['stable_samples']:]):
                    raise ValueError('No validated first-port echo before migration')
            windows=[]; failure=None
            for r in rows:
                if not r['ok'] and failure is None: failure=r
                elif r['ok'] and failure is not None:
                    windows.append(dict(first_failure_utc=failure['utc'],recovery_utc=r['utc'],
                        observed_seconds=r['mono']-failure['mono'],recovered=True)); failure=None
            if failure: windows.append(dict(first_failure_utc=failure['utc'],observed_seconds=None,recovered=False))
            post=[r for r in rows if r['seq']>recovery['client_fence']]
            if len(post)<cfg['stable_samples']:
                raise ValueError('Missing echoes after post-OVN TCP client sequence fence')
            recovered=all(r['ok'] for r in post[-cfg['stable_samples']:])
            results[str(port)]=dict(status='PASS' if recovered else 'FAIL',attempts=len(rows),
                validated_echoes=sum(r['ok'] for r in rows),failures=sum(not r['ok'] for r in rows),
                pre_activation_attempts=len(all_rows)-len(rows),failure_windows=windows,recovery='PASS' if recovered else 'FAIL')
        return dict(status='PASS' if all(r['status']=='PASS' for r in results.values()) else 'FAIL',enabled=True,
            run_id=session['run_id'],server=catalog['ew-app'],client=catalog['ew-client-b'],ports=results,
            server_boot=session['guests']['ew-app']['boot'],client_boot=session['guests']['ew-client-b']['boot'],
            activation=activation,recovery_anchor=recovery,
            timing_scope='client monotonic sampled connection/validated echo availability; independent of application and Pair-A PCAP')
    except FileNotFoundError: return dict(status='UNAVAILABLE',enabled=True,reason='Missing TCP experiment evidence')
    except (ValueError,KeyError,TypeError,AttributeError,OSError,StopIteration,RuntimeError) as exc:
        return dict(status='UNAVAILABLE',enabled=True,reason=str(exc))
