"""Combine collected per-client summaries; no third-party dependencies."""
import argparse
import json
import math
from pathlib import Path

CLIENTS = ("ew-client-a1", "ew-client-a2", "ew-client-b")


def assess(directory, mode):
    """Raw coverage first. Missing evidence is not an observed application loss."""
    directory=Path(directory)
    try:
        cfg=json.loads((directory/'config.json').read_text())
        state=json.loads((directory/'state.json').read_text())
        summary=json.loads((directory/'summary.json').read_text())
        rows=[json.loads(line) for line in (directory/'events.jsonl').read_text().splitlines()]
        if state['status']!='COMPLETE' or cfg!=state['config'] or cfg['boot']!=summary['guest_boot']:
            raise ValueError('Incomplete lifecycle/configuration/boot evidence')
        if cfg['mode']!=mode or summary['run_id']!=cfg['run_id'] or summary['client_id']!=cfg['client_id']:
            raise ValueError('Run/role/mode identity mismatch')
        if not rows or rows[0]['kind']!='run_start' or rows[-1]['kind']!='run_end' or not rows[-1]['probe_threads_stopped']:
            raise ValueError('Missing process endpoints or crashed probe thread')
        if [r['seq'] for r in rows]!=list(range(1,summary['last_event_seq']+1)):
            raise ValueError('Missing/duplicated raw events')
        monos=[r['mono'] for r in rows]
        if any(type(t) not in (int,float) or not math.isfinite(t) for t in monos) or any(b<a for a,b in zip(monos,monos[1:])):
            raise ValueError('Invalid process monotonic continuity')
        counts=summary['counts']; probes={}
        for kind in ('created','accepted','completed','slo_missed','integrity_error','unresolved'):
            if counts[kind]!=sum(r['kind']==kind for r in rows): raise ValueError('Raw task counters do not match summary')
        for spec in cfg['probes']:
            samples=[r for r in rows if r['kind']==spec['name']]
            limit=spec['interval']+5  # 3s HTTP / <=2s ping bounds plus scheduler allowance
            gaps=[b['mono']-a['mono'] for a,b in zip(samples,samples[1:])]
            if (len(samples)<3 or samples[0]['mono']-monos[0]>limit or
                monos[-1]-samples[-1]['mono']>limit or any(gap>limit for gap in gaps)):
                raise ValueError('Missing samples/unexplained capture gap in '+spec['name'])
            windows=[]; opened=None; successes=0
            for row in samples:
                successes=successes+1 if row['ok'] else 0
                if not row['ok'] and opened is None: opened=row
                elif row['ok'] and opened is not None:
                    windows.append(dict(start_utc=opened['utc'],recovery_utc=row['utc'],
                        observed_outage_seconds=row['mono']-opened['mono'],recovered=True)); opened=None
            if opened: windows.append(dict(start_utc=opened['utc'],recovered=False,observed_outage_seconds=None))
            probes[spec['name']]=dict(samples=len(samples),failures=sum(not r['ok'] for r in samples),
                windows=windows,recovered=successes>=cfg.get('stable_samples',3),
                source_boundary_diagnostic=spec.get('source_boundary',False),endpoint=spec,
                timing_scope='sampled availability, one process monotonic clock; not packet-level Pair-A outage')
        required=[p for p in probes.values() if mode=='baseline' or not p['source_boundary_diagnostic']]
        integrity=counts['integrity_error']==0
        reconciled=True
        if cfg['tasks']:
            stats=summary['server_stats'] or {}
            reconciled=(counts['created']>0 and counts['created']==counts['accepted']==counts['completed'] and
                        all(stats.get(k)==counts['created'] for k in ('accepted','done','processed')) and stats.get('pending')==0)
        recovery=all(p['recovered'] for p in required)
        migration=integrity and not counts['unresolved'] and reconciled and recovery
        baseline=migration and not counts['slo_missed'] and not counts['http_failures'] and all(p['failures']==0 for p in required)
        return dict(status='PASS' if (baseline if mode=='baseline' else migration) else 'FAIL',coverage='PASS',
            baseline_acceptance='PASS' if baseline else 'FAIL',migration_acceptance='PASS' if migration else 'FAIL',
            probes=probes,counts=counts,rates_per_second=summary['rates_per_second'],
            offered_load_model='closed loop, one in-flight task per client; max-rate is a ceiling',
            integrity='PASS' if integrity else 'FAIL',task_reconciliation='PASS' if reconciled else 'FAIL',
            task_outcomes=dict(payload_corruption=sum(r.get('payload_corruption') is True for r in rows),
                invalid_processing_count=sum(r.get('process_count_invalid') is True for r in rows),
                repeated_processing=sum(type(r.get('process_count')) is int and r['process_count']>1 for r in rows),
                completed_with_redelivery=sum(r['kind']=='completed' and type(r.get('delivery_count')) is int and r['delivery_count']>1 for r in rows)),
            recovery='PASS' if recovery else 'FAIL',e2e_latency_ms=summary['e2e_latency_ms'],
            server_stats=summary['server_stats'],placement=cfg['placement'])
    except (OSError,ValueError,KeyError,TypeError) as exc:
        return dict(status='UNAVAILABLE',coverage='UNAVAILABLE',reason=str(exc),
                    baseline_acceptance='UNAVAILABLE',migration_acceptance='UNAVAILABLE')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    parser.add_argument('--mode',choices=('baseline','migration'))
    args = parser.parse_args()
    directory = Path(args.directory)
    if (directory/CLIENTS[0]/'config.json').exists() or args.mode:
        mode=args.mode or json.loads((directory/CLIENTS[0]/'config.json').read_text())['mode']
        actors={name:assess(directory/name,mode) for name in (*CLIENTS,'ew-app')}
        combined=dict(mode=mode,status='PASS' if all(a['status']=='PASS' for a in actors.values()) else
                      'FAIL' if any(a['status']=='FAIL' for a in actors.values()) else 'UNAVAILABLE',actors=actors)
        (directory/'combined-summary.json').write_text(json.dumps(combined,indent=2)+'\n')
        print(json.dumps(combined,indent=2)); return 0 if combined['status']=='PASS' else 1
    summaries = []
    for client in CLIENTS:
        path = directory / client / "summary.json"
        if not path.is_file():
            raise SystemExit("Missing summary: " + str(path))
        summary = json.loads(path.read_text())
        if summary["client_id"] != client:
            raise SystemExit("Unexpected client identity in " + str(path))
        summaries.append(summary)
    if len({item["run_id"] for item in summaries}) != 1:
        raise SystemExit("Cannot combine different runs")
    combined = {"run_id": summaries[0]["run_id"],
                "baseline_passed": all(item["baseline_passed"] for item in summaries),
                "clients": summaries}
    (directory / "combined-summary.json").write_text(json.dumps(combined, indent=2) + "\n")
    print("BASELINE_RUN_ID=" + combined["run_id"])
    print()
    print("| Client | Created | Completed | HTTP errors | Ping fail/total | TCP fail/total | p95 ms | p99 ms |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for item in summaries:
        c, p, latency = item["counts"], item["probes"], item["e2e_latency_ms"]
        print(f"| {item['client_id']} | {c['created']} | {c['completed']} | {c['http_failures']} | "
              f"{p['ping']['failures']}/{p['ping']['samples']} | {p['tcp']['failures']}/{p['tcp']['samples']} | "
              f"{latency['p95']} | {latency['p99']} |")
    print()
    for item in summaries:
        c = item["counts"]
        print("CLIENT_DETAIL " + json.dumps({
            "client": item["client_id"], "slo_missed": c["slo_missed"],
            "integrity_errors": c["integrity_error"], "unresolved": c["unresolved"],
            "server_stats": item["server_stats"], "reconciliation_error": item["reconciliation_error"],
        }, sort_keys=True))
    print("BASELINE_OK" if combined["baseline_passed"] else "BASELINE_NEEDS_REVIEW")
    return 0 if combined["baseline_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
