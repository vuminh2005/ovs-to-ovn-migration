"""Closed-loop EW workload and independent ping/TCP probes; stdlib only."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener
import uuid


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def percentile(values, percent):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * percent / 100) - 1)], 3)


class Recorder:
    def __init__(self, directory, probe_names=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stream = (self.directory / "events.jsonl").open("x", encoding="utf-8")
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.counts = Counter()
        self.http_statuses = Counter()
        self.latencies = []
        self.sequence = 0
        self.probes = {name: {"samples": 0, "failures": 0, "open_failure": None,
                              "last_sample_elapsed_s": None, "last_sample_utc": None,
                              "failure_windows": [], "last_successes": 0,
                              "maximum_sample_gap_seconds": 0} for name in (probe_names or ("ping", "tcp"))}

    def event(self, kind, **fields):
        with self.lock:
            elapsed = time.monotonic() - self.started
            self.sequence += 1
            record = {"seq": self.sequence, "utc": utc(), "mono": time.monotonic(),
                      "elapsed_s": round(elapsed, 6), "kind": kind, **fields}
            self.stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.stream.flush()
            if kind in ("created", "accepted", "completed", "slo_missed", "integrity_error", "unresolved"):
                self.counts[kind] += 1
            if kind == "completed":
                self.latencies.append(fields["latency_ms"])
            if kind == "http":
                self.counts["http_attempts"] += 1
                self.http_statuses[str(fields["status"])] += 1
                if fields["error"] or fields["status"] >= 400:
                    self.counts["http_failures"] += 1
            if kind in self.probes:
                probe = self.probes[kind]
                probe["samples"] += 1
                if probe['last_sample_elapsed_s'] is not None:
                    probe['maximum_sample_gap_seconds'] = max(probe['maximum_sample_gap_seconds'], elapsed-probe['last_sample_elapsed_s'])
                probe['last_successes'] = probe['last_successes']+1 if fields['ok'] else 0
                probe.setdefault('first_sample_elapsed_s', elapsed)
                probe["last_sample_elapsed_s"] = elapsed
                probe["last_sample_utc"] = record["utc"]
                if not fields["ok"]:
                    probe["failures"] += 1
                    if probe["open_failure"] is None:
                        probe["open_failure"] = {"start_elapsed_s": elapsed, "start_utc": record["utc"]}
                elif probe["open_failure"] is not None:
                    window = probe["open_failure"]
                    window.update(end_elapsed_s=elapsed, end_utc=record["utc"],
                                  observed_seconds=round(elapsed - window["start_elapsed_s"], 6),
                                  recovery_observed=True)
                    probe["failure_windows"].append(window)
                    probe["open_failure"] = None
            return record

    def finish(self, metadata, server_stats, reconciliation_error):
        with self.lock:
            elapsed = time.monotonic() - self.started
            for probe in self.probes.values():
                if probe["open_failure"] is not None:
                    window = probe["open_failure"]
                    window.update(end_elapsed_s=probe["last_sample_elapsed_s"], end_utc=probe["last_sample_utc"],
                                  observed_seconds=round(probe["last_sample_elapsed_s"] - window["start_elapsed_s"], 6),
                                  recovery_observed=False)
                    probe["failure_windows"].append(window)
                    probe["open_failure"] = None
            counts = {key: self.counts[key] for key in (
                "created", "accepted", "completed", "slo_missed", "integrity_error",
                "unresolved", "http_attempts", "http_failures")}
            passed = (
                counts["created"] > 0 and counts["created"] == counts["completed"]
                and not any(counts[key] for key in ("slo_missed", "integrity_error", "unresolved", "http_failures"))
                and all(probe["samples"] > 0 and probe["failures"] == 0 for probe in self.probes.values())
                and server_stats is not None
                and all(server_stats.get(key) == counts["created"] for key in ("accepted", "done", "processed"))
                and server_stats.get("pending") == 0
            )
            summary = {
                **metadata, "finished_utc": utc(), "actual_seconds": round(elapsed, 3),
                "mode": "closed_loop_one_inflight", "counts": counts,
                "http_statuses": dict(self.http_statuses), "probes": self.probes,
                "e2e_latency_ms": {"samples": len(self.latencies),
                    "p50": percentile(self.latencies, 50), "p95": percentile(self.latencies, 95),
                    "p99": percentile(self.latencies, 99),
                    "max": round(max(self.latencies), 3) if self.latencies else None},
                "server_stats": server_stats, "reconciliation_error": reconciliation_error,
                "baseline_passed": bool(passed),
                "last_event_seq": self.sequence, "probe_interval_seconds": metadata.get('probe_interval_seconds', 1),
                "rates_per_second": {k: round(counts[k]/elapsed, 6) if elapsed else None
                                     for k in ('created', 'accepted', 'completed')},
            }
            summary['rates_per_second']['attempted']=summary['rates_per_second']['created']
            self.stream.close()
            temporary = self.directory / "summary.json.tmp"
            temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
            temporary.replace(self.directory / "summary.json")
            return summary


class Client:
    def __init__(self, url, recorder, timeout=3):
        self.url = url.rstrip("/")
        self.recorder = recorder
        self.opener = build_opener(ProxyHandler({}))
        self.timeout = timeout

    def request(self, path, task=None, phase="poll", log=True):
        started = time.monotonic()
        raw = None if task is None else json.dumps(task).encode("utf-8")
        headers = {} if task is None else {"Content-Type": "application/json"}
        status, result, error = 0, None, None
        try:
            request = Request(self.url + path, data=raw, headers=headers)
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except HTTPError as exc:
                response = exc
            with response:
                status = response.code
                result = json.loads(response.read(1048576))
        except Exception as exc:
            error = type(exc).__name__
        if log:
            self.recorder.event("http", phase=phase, status=status, error=error,
                                duration_ms=round((time.monotonic() - started) * 1000, 3))
        return status, result, error


def run_task(client, task, recorder, deadline, slo_seconds):
    started = time.monotonic()
    accepted, missed = False, False
    expected = hashlib.sha256(task["payload"].encode("utf-8")).hexdigest()
    recorder.event("created", task_id=task["task_id"], payload_sha256=expected)

    def check_slo():
        nonlocal missed
        if not missed and time.monotonic() - started > slo_seconds:
            missed = True
            recorder.event("slo_missed", task_id=task["task_id"])

    while time.monotonic() < deadline():
        check_slo()
        phase = "poll" if accepted else "submit"
        path = "/jobs/" + task["task_id"] if accepted else "/jobs"
        status, result, error = client.request(path, None if accepted else task, phase=phase)
        if error or status not in (200, 202):
            if not error and 400 <= status < 500:
                recorder.event("integrity_error", task_id=task["task_id"], reason="unexpected_http_4xx", status=status)
                return False
            time.sleep(1)
            continue
        job = result.get("job") if isinstance(result, dict) else None
        if not isinstance(job, dict) or job.get("task_id") != task["task_id"] or job.get("status") not in ("pending", "done"):
            recorder.event("integrity_error", task_id=task["task_id"], reason="invalid_job_response")
            return False
        if not accepted:
            accepted = True
            recorder.event("accepted", task_id=task["task_id"])
        if job["status"] == "done":
            check_slo()
            if job.get("result_sha256") != expected or job.get("process_count") != 1:
                recorder.event("integrity_error", task_id=task["task_id"], reason="result_or_process_count_mismatch",
                               payload_corruption=job.get('result_sha256')!=expected,
                               process_count_invalid=job.get('process_count')!=1,
                               process_count=job.get('process_count'))
                return False
            recorder.event("completed", task_id=task["task_id"],
                           latency_ms=round((time.monotonic() - started) * 1000, 3),
                           delivery_count=job.get("delivery_count"),process_count=job.get('process_count'))
            return True
        time.sleep(0.25)
    check_slo()
    recorder.event("unresolved", task_id=task["task_id"], accepted_response_seen=accepted)
    return False


def probe(kind, host, port, payload=56, df=False):
    started = time.monotonic()
    error = None
    try:
        if kind == "ping":
            completed = subprocess.run(
                ["ping", "-4", "-n", "-c", "1", "-W", "1", "-M", "do" if df else "dont", "-s", str(payload), host],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
            )
            ok = completed.returncode == 0
        else:
            with socket.create_connection((host, port), timeout=1):
                ok = True
    except Exception as exc:
        ok, error = False, type(exc).__name__
    return ok, round((time.monotonic() - started) * 1000, 3), error


def probe_loop(kind, host, port, recorder, stop_event):
    while not stop_event.is_set():
        started = time.monotonic()
        ok, elapsed, error = probe(kind, host, port)
        recorder.event(kind, ok=ok, duration_ms=elapsed, error=error)
        stop_event.wait(max(0, 1 - (time.monotonic() - started)))


def atomic_json(path, value, mode=None):
    path = Path(path); temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2); stream.flush(); os.fsync(stream.fileno())
    if mode is not None: os.chmod(temporary,mode)
    temporary.replace(path)


def process_identity(pid):
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]


def managed_probe(spec, recorder, stop):
    client = Client(spec.get('url', ''), recorder)
    while not stop.is_set():
        started = time.monotonic()
        if spec['type'] in ('live', 'dependency'):
            code, result, error = client.request('/live' if spec['type']=='live' else '/health', log=False)
            ok = not error and code == 200 and isinstance(result, dict)
            if spec['type']=='dependency':
                ok = ok and result.get('dependencies') == {'postgresql':'ok', 'rabbitmq':'ok'}
            elapsed = (time.monotonic()-started)*1000
        else:
            ok, elapsed, error = probe('tcp' if spec['type']=='tcp' else 'ping', spec['host'], spec['port'],
                                       spec.get('payload',56), spec['type']=='df')
        recorder.event(spec['name'], ok=bool(ok), duration_ms=elapsed, error=error,
                       endpoint=spec.get('url',spec['host']), probe_type=spec['type'],
                       expected_boundary_failure=spec.get('source_boundary',False))
        stop.wait(max(0, spec['interval']-(time.monotonic()-started)))


def tcp_listeners(cfg, recorder, stop):
    """Two listeners owned by this bounded runner; bind is the activation proof."""
    import selectors
    directory=Path(cfg['output']); experiment=cfg['tcp_experiment']; listeners={}; activated={}
    request=directory/'tcp-second.request.json'
    with selectors.DefaultSelector() as selector:
        try:
            while not stop.is_set():
                wanted=[experiment['ports'][0]]
                if request.exists():
                    intent=json.loads(request.read_text())
                    if intent!=dict(run_id=cfg['run_id'],boot=cfg['boot'],port=experiment['ports'][1]):
                        raise RuntimeError('TCP activation intent identity mismatch')
                    wanted.append(experiment['ports'][1])
                for port in wanted:
                    if port in listeners: continue
                    sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
                    try:
                        sock.bind((cfg['ip'],port)); sock.listen(8); sock.setblocking(False)
                    except Exception as exc:
                        sock.close(); recorder.event('tcp_listener_failed',port=port,error_type=type(exc).__name__,run_id=cfg['run_id'],boot=cfg['boot'])
                        raise
                    listeners[port]=sock; selector.register(sock,selectors.EVENT_READ)
                    row=recorder.event('tcp_listener_activated',port=port,run_id=cfg['run_id'],boot=cfg['boot'])
                    activated[str(port)]=dict(port=port,run_id=cfg['run_id'],boot=cfg['boot'],seq=row['seq'])
                    atomic_json(directory/'tcp-listeners.json',activated)
                for key,_ in selector.select(.1):
                    conn,_=key.fileobj.accept()
                    with conn:
                        conn.settimeout(1)
                        try:
                            value=tcp_receive(conn)
                            if value.get('run_id')!=cfg['run_id'] or not isinstance(value.get('nonce'),str): continue
                            conn.sendall(json.dumps(dict(value,boot=cfg['boot'])).encode()+b'\n')
                        except (OSError,ValueError,AttributeError): pass
        finally:
            for sock in listeners.values(): sock.close()


def tcp_receive(conn):
    deadline=time.monotonic()+1; data=b''
    while not data.endswith(b'\n') and len(data)<=512:
        remaining=deadline-time.monotonic()
        if remaining<=0: raise TimeoutError('TCP echo frame deadline')
        conn.settimeout(remaining); chunk=conn.recv(513-len(data))
        if not chunk: raise ValueError('Incomplete TCP echo frame')
        data+=chunk
    if len(data)>512: raise ValueError('Oversized TCP echo frame')
    return json.loads(data)


def tcp_echo_attempt(host,port,run_id,server_boot):
    nonce=uuid.uuid4().hex
    with socket.create_connection((host,port),timeout=1) as conn:
        conn.settimeout(1); conn.sendall(json.dumps(dict(run_id=run_id,nonce=nonce)).encode()+b'\n')
        response=tcp_receive(conn)
        return response==dict(run_id=run_id,nonce=nonce,boot=server_boot)


def tcp_echo_loop(cfg, recorder, stop):
    experiment=cfg['tcp_experiment']
    while not stop.is_set():
        started=time.monotonic()
        for port in experiment['ports']:
            error=None
            try:
                ok=tcp_echo_attempt(experiment['server_ip'],port,cfg['run_id'],experiment['server_boot'])
                if not ok: error='InvalidEcho'
            except (OSError,ValueError) as exc: ok=False; error=type(exc).__name__
            recorder.event('tcp_echo',port=port,ok=ok,error=error,run_id=cfg['run_id'],boot=cfg['boot'],
                server_boot=experiment['server_boot'],validation='run_id_nonce_server_boot')
        stop.wait(max(0,cfg['interval']-(time.monotonic()-started)))


def managed_main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(); parser.add_argument('--config', required=True)
    cfg = json.loads(Path(parser.parse_args().config).read_text())
    directory = Path(cfg['output']); state_path = directory/'state.json'
    state = json.loads(state_path.read_text())
    if state['status'] != 'START_REQUESTED' or state['config'] != cfg:
        raise RuntimeError('Runner intent/configuration mismatch; never restart an ambiguous run')
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if boot != cfg['boot']:
        raise RuntimeError('Guest rebooted after launch intent')
    recorder = Recorder(directory, [p['name'] for p in cfg['probes']])
    start = time.monotonic(); stop_creating = threading.Event(); stop_probes = threading.Event()
    active_duration = cfg['duration'] or cfg['maximum_lifetime']
    active_end = start+min(active_duration,cfg['maximum_lifetime'])
    deadlines = [active_end+cfg['drain']]
    def stop_requested(*_args):
        stop_creating.set(); deadlines[0] = min(deadlines[0],time.monotonic()+cfg['drain'])
    signal.signal(signal.SIGTERM, stop_requested); signal.signal(signal.SIGINT, stop_requested)
    state.update(status='RUNNING', pid=os.getpid(), process_identity=process_identity(os.getpid()),
                 started_utc=utc(), started_mono=start, boot=boot)
    atomic_json(state_path,state)
    metadata = dict(cfg, started_utc=state['started_utc'], probe_interval_seconds=cfg['interval'],
                    database_run_id=cfg['database_run_id'], guest_boot=boot)
    recorder.event('run_start', **metadata)
    def heartbeat():
        while not stop_probes.wait(.5):
            if (directory/'stop.request').exists() or time.monotonic() >= active_end:
                stop_requested()
            state.update(status='DRAINING' if stop_creating.is_set() else 'RUNNING', heartbeat_mono=time.monotonic())
            atomic_json(state_path,state)
    thread_failures = []
    def guarded(target, name, *args):
        try: target(*args)
        except Exception as exc:
            thread_failures.append(name)
            recorder.event('measurement_thread_crash', thread=name, error_type=type(exc).__name__)
            stop_requested()  # retain evidence; never restart a failed measurement thread
    threads = [threading.Thread(target=guarded,args=(managed_probe,p['name'],p,recorder,stop_probes),daemon=True) for p in cfg['probes']]
    threads.append(threading.Thread(target=guarded,args=(heartbeat,'heartbeat'),daemon=True))
    if cfg.get('tcp_experiment'):
        target=tcp_listeners if cfg['client_id']=='ew-app' else tcp_echo_loop
        threads.append(threading.Thread(target=guarded,args=(target,'tcp_experiment',cfg,recorder,stop_probes),daemon=True))
    for thread in threads: thread.start()
    client = Client(cfg['url'],recorder)
    try:
        while not stop_creating.is_set() and time.monotonic()<active_end:
            task_start = time.monotonic()
            if cfg['tasks']:
                task_id = str(uuid.uuid4())
                task = dict(task_id=task_id,run_id=cfg['database_run_id'],client_id=cfg['client_id'],payload=('EW:'+task_id+':')*256)
                run_task(client,task,recorder,lambda: deadlines[0],cfg['slo'])
            stop_creating.wait(max(0,1/cfg['max_rate']-(time.monotonic()-task_start)))
    finally:
        stop_probes.set()
        for thread in threads: thread.join(timeout=5)
    code, stats, error = client.request('/stats?'+urlencode({'run_id':cfg['database_run_id']}),log=False) if cfg['tasks'] else (200,{},None)
    if error or code != 200 or not isinstance(stats,dict): stats,error=None,error or f'HTTP_{code}'
    recorder.event('run_end', reason='requested_stop' if (directory/'stop.request').exists() else 'bounded_duration',
                   probe_threads_stopped=not thread_failures and all(not t.is_alive() for t in threads))
    summary = recorder.finish(metadata,stats,error)
    state.update(status='COMPLETE', finished_utc=utc(), finished_mono=time.monotonic(), last_event_seq=summary['last_event_seq'])
    atomic_json(state_path,state)
    return 0  # completed measurement; acceptance is evaluated separately


def main():
    # The managed lifecycle passes immutable per-run configuration. Old finite
    # runner CLI remains available for source compatibility.
    if '--config' in os.sys.argv:
        return managed_main()
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://192.168.101.11:8080")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--duration", type=float, default=120, help="0 means until SIGTERM")
    parser.add_argument("--drain", type=float, default=30)
    parser.add_argument("--max-rate", type=float, default=1)
    parser.add_argument("--slo", type=float, default=10)
    args = parser.parse_args()
    if args.duration < 0 or args.drain < 0 or args.max_rate <= 0 or args.slo <= 0:
        parser.error("invalid duration, drain, max-rate or slo")
    if not all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) for value in (args.client_id, args.run_id)):
        parser.error("client-id/run-id must have 1 to 64 safe characters")
    database_run = args.run_id + "-" + args.client_id
    if len(database_run) > 64:
        parser.error("combined database run ID exceeds 64 characters")
    os.umask(0o077)
    recorder = Recorder(args.output)
    started = time.monotonic()
    stop_creating = threading.Event()
    stop_probes = threading.Event()
    hard_end = started + args.duration + args.drain if args.duration else math.inf
    signal_deadline = [math.inf]

    def on_signal(_signum, _frame):
        stop_creating.set()
        signal_deadline[0] = min(signal_deadline[0], time.monotonic() + args.drain)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    metadata = {
        "run_id": args.run_id, "database_run_id": database_run, "client_id": args.client_id,
        "started_utc": utc(), "requested_duration_seconds": args.duration,
        "drain_seconds": args.drain, "max_jobs_per_second": args.max_rate,
        "slo_seconds": args.slo, "api_url": args.url,
    }
    recorder.event("run_start", **metadata)
    parsed = urlsplit(args.url)
    threads = [threading.Thread(target=probe_loop, args=(kind, parsed.hostname, parsed.port or 80, recorder, stop_probes))
               for kind in ("ping", "tcp")]
    for thread in threads:
        thread.start()
    client = Client(args.url, recorder)
    try:
        while not stop_creating.is_set() and (not args.duration or time.monotonic() - started < args.duration):
            task_started = time.monotonic()
            task_id = str(uuid.uuid4())
            task = {"task_id": task_id, "run_id": database_run, "client_id": args.client_id,
                    "payload": ("EW:" + task_id + ":") * 256}
            run_task(client, task, recorder, lambda: min(hard_end, signal_deadline[0]), args.slo)
            pause = max(0, 1 / args.max_rate - (time.monotonic() - task_started))
            if args.duration:
                pause = min(pause, max(0, started + args.duration - time.monotonic()))
            stop_creating.wait(pause)
    finally:
        stop_probes.set()
        for thread in threads:
            thread.join()
    code, stats, error = client.request("/stats?" + urlencode({"run_id": database_run}), log=False)
    if error or code != 200 or not isinstance(stats, dict):
        stats, error = None, error or f"HTTP_{code}"
    summary = recorder.finish(metadata, stats, error)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary["baseline_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
