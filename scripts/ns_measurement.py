"""Independent N-S observer metrics. Single-observer monotonic sampled windows."""
import json
import hashlib
import math
import pathlib
from ew_tcp_experiment import restoration_markers
from ew_transport import verify_profile
from ns_scope import preserved


def read(root, name, default=None):
    p=pathlib.Path(root)/name
    return json.loads(p.read_text()) if p.exists() else default


def enabled(root):
    runtime=read(root,'runtime.json',{})
    return runtime.get('ns_enabled',False) is True


def observed_windows(rows):
    windows=[]; burst=None
    for row in rows:
        if not row['http_success']:
            if burst is None: burst=dict(start_monotonic=row['mono'],start_sequence=row['seq'],failures=0,recovery_monotonic=None,duration_seconds=None)
            burst['failures']+=1
        elif burst is not None:
            burst.update(recovery_monotonic=row['http_end'],recovery_sequence=row['seq'],duration_seconds=row['http_end']-burst['start_monotonic'])
            windows.append(burst); burst=None
    if burst: windows.append(burst)
    return windows


def metrics(raw, cfg, start, end, fence):
    result=dict(status='UNAVAILABLE',coverage='UNAVAILABLE',attempts=None,successes=None,failures=None,
                longest_recovered_outage_seconds=None,sampled_outage_windows=[],latency_seconds=None,
                measurement='fresh HTTP requests; sampled observations, not packet loss or conntrack preservation',
                start_anchor=start,end_anchor=end,recovery_fence=fence,diagnostics_only=True)
    try:
        rows=raw['rows']; state=raw['state']
        if state['config']!=cfg or state['boot']!=cfg['boot'] or state['status'] not in ('RUNNING','STOPPED','EXPIRED'):
            raise ValueError('observer identity/state differs')
        if (type(state['seq']) is not int or not rows or
                any(not isinstance(r,dict) or type(r.get('seq')) is not int or r['seq']<=0 for r in rows)):
            raise ValueError('malformed append-only sequence evidence')
        seqs=[r['seq'] for r in rows]
        if seqs!=sorted(set(seqs)) or seqs[-1]>state['seq']:
            raise ValueError('ambiguous append-only sequence ordering/checkpoint')
        by_seq={r['seq']:r for r in rows}
        def finite(v): return type(v) in (int,float) and math.isfinite(v)
        def row_issue(row):
            try:
                if (row['run']!=cfg['run'] or row['direction']!=cfg['direction'] or row['boot']!=cfg['boot'] or
                    not all(finite(row[k]) for k in ('mono','http_end','end_mono')) or
                    not row['mono']<=row['http_end']<=row['end_mono'] or type(row['http_success']) is not bool or
                    row['end_mono']-row['mono']>2*cfg['timeout']+2):
                    return 'invalid observation identity/timing'
            except (KeyError,TypeError): return 'malformed observation'
        for anchor in (start,end):
            if anchor is None: continue
            if type(anchor['seq']) is not int or anchor['seq'] not in by_seq:
                raise ValueError('missing immutable anchor observation')
            row=by_seq[anchor['seq']]
            if row_issue(row) or anchor!={'seq':row['seq'],'mono':row['end_mono'],'boot':row['boot']}:
                raise ValueError('anchor differs from valid raw observer evidence')
        lower=start['seq'] if start else 0
        upper=end['seq'] if end else state['seq']
        selected=[r for r in rows if lower<r['seq']<=upper]
        outside=[]; result['outside_window_anomalies']=outside
        for row in rows:
            issue=row_issue(row)
            if lower<=row['seq']<=upper:
                if issue: raise ValueError(issue)
            elif issue:
                outside.append(dict(sequence=row['seq'],location='before_start' if row['seq']<lower else 'after_end',reason=issue))
        # Only continuity within (start, end], including its start transition, is
        # authoritative. Earlier/later anomalies remain visible without moving anchors.
        previous=by_seq.get(lower)
        for row in selected:
            if previous and not 0<=row['mono']-previous['end_mono']<=cfg['interval']+.5:
                result['reason']='unexplained observer gap within measurement interval'
            previous=row
        for previous,row in zip(rows,rows[1:]):
            if row['seq']<=lower or row['seq']>upper:
                location='before_start' if row['seq']<=lower else 'after_end'
                if row['seq']!=previous['seq']+1:
                    outside.append(dict(sequence=row['seq'],location=location,reason='missing outside-window observations'))
                if not row_issue(previous) and not row_issue(row) and not 0<=row['mono']-previous['end_mono']<=cfg['interval']+.5:
                    outside.append(dict(sequence=row['seq'],location=location,reason='unexplained observer gap'))
        if seqs[0]>1 and lower>=seqs[0]:
            outside.append(dict(sequence=seqs[0],location='before_start',reason='missing outside-window prefix'))
        tail_issue=state['seq']!=seqs[-1] or state.get('last_mono')!=rows[-1].get('end_mono')
        if tail_issue:
            if upper>=state['seq']: raise ValueError('observer checkpoint/tail differs')
            outside.append(dict(sequence=state['seq'],location='after_end',reason='observer checkpoint/tail differs'))
        result['coverage_max_idle_seconds']=cfg['interval']+.5
        result['observed_samples']=dict(attempts=len(selected),successes=sum(r['http_success'] for r in selected),failures=sum(not r['http_success'] for r in selected))
        result['sampled_outage_windows']=observed_windows(selected)
        if result.get('reason'): raise ValueError(result['reason'])
        if (len(selected)!=upper-lower or (selected and (selected[0]['seq']!=lower+1 or selected[-1]['seq']!=upper))):
            raise ValueError('incomplete append-only sequence coverage within measurement interval')
        if not start or not end or any(type(v) is not int for v in (start['seq'],fence,end['seq'])) or not start['seq']<fence<end['seq']<=state['seq']:
            raise ValueError('missing or invalid immutable measurement/recovery anchors; observed open windows retained')
        tail=selected[-cfg['stable_samples']:]
        if len(tail)!=cfg['stable_samples'] or not all(r['seq']>fence and r['http_success'] and (not cfg['probe'].get('session_port') or (r.get('session') or {}).get('success') is True) for r in tail):
            raise ValueError('no stable fresh post-OVN recovery')
        successes=sum(r['http_success'] for r in selected); windows=observed_windows(selected)
        burst=bool(windows and windows[-1]['recovery_monotonic'] is None)
        latencies=sorted(r['http_end']-r['mono'] for r in selected if r['http_success'])
        sessions=[r['session'] for r in selected if r.get('session')]
        result.update(attempts=len(selected),successes=successes,failures=len(selected)-successes,
                      sampled_outage_windows=windows,coverage='PASS',
                      failure_percent=100*(len(selected)-successes)/len(selected),
                      session=dict(semantics='observed established echo connection failures, not wire TCP RST counts',measured=bool(cfg['probe'].get('session_port')),resets=sum(r['reset'] for r in sessions),
                                   connections_opened=sum(r['opened'] for r in sessions),reconnections=sum(r['opened'] and r.get('connection',1)>1 for r in sessions),
                                   validated_echoes=sum(r['success'] for r in sessions),sampled_outage_windows=observed_windows([dict(r,http_success=r['session']['success'],http_end=r['end_mono']) for r in selected if r.get('session')])),
                      latency_seconds=dict(min=min(latencies),max=max(latencies),mean=sum(latencies)/len(latencies),p95=latencies[max(0,math.ceil(.95*len(latencies))-1)]))
        if burst: raise ValueError('open-ended outage has no valid recovery')
        result.update(status='PASS',diagnostics_only=False,longest_recovered_outage_seconds=max([w['duration_seconds'] for w in windows] or [0.0]))
    except (ValueError,KeyError,TypeError,AttributeError,IndexError,ZeroDivisionError) as exc:
        result['reason']=str(exc)
    return result


def report(root):
    if not enabled(root): return dict(enabled=False,status='NOT TESTED')
    result=dict(enabled=True,status='UNAVAILABLE',directions={})
    errors=(OSError,RuntimeError,ValueError,KeyError,TypeError,AttributeError,IndexError)
    try:
        cfg=read(root,'ns-config.json'); lifecycle=read(root,'ns-lifecycle.json')
        if not cfg or not lifecycle or lifecycle.get('config_sha256')!=hashlib.sha256(json.dumps(cfg,sort_keys=True).encode()).hexdigest():
            raise ValueError('missing or changed configuration/lifecycle binding')
    except errors as exc:
        result['reason']=str(exc); return result
    source_errors=[]
    try: before=read(root,'ns-before.json')
    except errors as exc: before=None; source_errors.append('ns-before.json: '+str(exc))
    try: baseline=read(root,'ns-guest-baselines.json')
    except errors as exc: baseline=None; source_errors.append('ns-guest-baselines.json: '+str(exc))
    expected={'egress','ingress'} if cfg.get('ingress') else {'egress'}
    recovery_error=None
    try: recovery=read(root,'ns-recovery.json') or {}
    except errors as exc: recovery={}; recovery_error=str(exc)
    # Extract independently trustworthy observations first. Recovery and target
    # health gates authorize acceptance, not the visibility of observed failures.
    for direction in sorted(expected):
        measured=dict(status='UNAVAILABLE',coverage='UNAVAILABLE',diagnostics_only=True,
            attempts=None,successes=None,failures=None,sampled_outage_windows=[],
            longest_recovered_outage_seconds=None,latency_seconds=None)
        try:
            entry=lifecycle['observers'][direction]
            expected_source=cfg['egress']['guest']['ip'] if direction=='egress' else cfg['ingress']['observer']['ip']
            expected_peer=before['gateway']['fixed_ips'][0]['ip_address'] if direction=='egress' else expected_source
            if (entry['config']['run']!=pathlib.Path(root).name or entry['config']['direction']!=direction or
                entry['config']['source_ip']!=expected_source or entry['config']['expected_peer']!=expected_peer or
                entry['config']['probe']!=cfg[direction]['probe'] or
                any(entry['config'][k]!=cfg[k] for k in ('interval','timeout','lifetime','stable_samples')) or
                entry['config']['boot']!=(baseline['egress']['boot'] if direction=='egress' else cfg['ingress']['observer']['boot'])):
                raise ValueError('observer endpoint/cadence/boot differs from checkpoint')
            raw=read(root,'ns-'+direction+'-raw.json')
            if raw is None: raise ValueError('missing raw observer evidence for '+direction)
            fences=recovery.get('fences') if isinstance(recovery,dict) else None
            fence=fences.get(direction) if isinstance(fences,dict) else None
            measured=metrics(raw,entry['config'],entry.get('start'),entry.get('end'),fence)
        except errors as exc: measured['reason']=str(exc)
        measured['path_identity']=cfg.get(direction)
        measured['cadence_seconds']=cfg.get('interval'); measured['timeout_seconds']=cfg.get('timeout')
        result['directions'][direction]=measured
    try:
        if source_errors: raise ValueError('invalid source identity evidence: '+'; '.join(source_errors))
        if recovery_error: raise ValueError('invalid recovery evidence: '+recovery_error)
        markers=restoration_markers(pathlib.Path(root))
        if (not recovery or recovery['status']!='PASS' or
                recovery['controller_markers']!=markers or not math.isfinite(recovery['established_at']) or
                recovery['established_at']<markers['control_plane_downtime.end'] or
                read(root,'ns-post-identities.json',{}).get('status')!='PASS' or
                read(root,'ns-ovn-takeover.json',{}).get('status')!='PASS'):
            raise ValueError('missing fresh ordered restoration, completed recovery, target identity or OVN gateway evidence')
        after=read(root,'ns-after.json'); preserved(before,after)
        identities=read(root,'ns-post-identities.json')
        if (identities['controller_markers']!=markers or not math.isfinite(identities['established_at']) or
                identities['established_at']<markers['control_plane_downtime.end']):
            raise ValueError('post-OVN identity evidence is stale')
        for direction in sorted(expected):
            vm=cfg[direction]['guest']; journal=read(root,'network-mtu-plan.json')['networks']
            mtu=[r['target_mtu'] for r in journal if r['network']==vm['network']]
            if len(mtu)!=1 or after['guest_networks'][direction]['type']!='geneve' or after['guest_networks'][direction]['mtu']!=mtu[0]:
                raise ValueError('target Geneve/MTU endpoint evidence missing')
            profile=identities['profiles']['egress' if direction=='egress' else 'ingress_guest']
            verify_profile(profile,vm,baseline[direction]['boot'],mtu[0],vm['mac'])
        if cfg.get('ingress'):
            observer=identities['profiles']['ingress']
            if observer['product_uuid']!=cfg['ingress']['observer']['product_uuid'] or observer['boot']!=cfg['ingress']['observer']['boot']:
                raise ValueError('external observer identity changed')
        if set(lifecycle['observers'])!=expected:
            raise ValueError('missing or unreviewed direction lifecycle')
    except errors as exc:
        result['reason']=str(exc)
        for measured in result['directions'].values():
            measured['measurement_status']=measured['status']
            measured['acceptance_reason']=str(exc)
            measured.update(status='UNAVAILABLE',diagnostics_only=True,attempts=None,successes=None,failures=None,
                            failure_percent=None,longest_recovered_outage_seconds=None,latency_seconds=None)
        return result
    if any(r['status']!='PASS' for r in result['directions'].values()):
        result['reason']='required direction evidence incomplete'
    else: result['status']='PASS'
    return result
