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
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stream = (self.directory / "events.jsonl").open("x", encoding="utf-8")
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.counts = Counter()
        self.http_statuses = Counter()
        self.latencies = []
        self.probes = {name: {"samples": 0, "failures": 0, "open_failure": None,
                              "last_sample_elapsed_s": None, "last_sample_utc": None,
                              "failure_windows": []} for name in ("ping", "tcp")}

    def event(self, kind, **fields):
        with self.lock:
            elapsed = time.monotonic() - self.started
            record = {"utc": utc(), "elapsed_s": round(elapsed, 6), "kind": kind, **fields}
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
            }
            self.stream.close()
            temporary = self.directory / "summary.json.tmp"
            temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
            temporary.replace(self.directory / "summary.json")
            return summary


class Client:
    def __init__(self, url, recorder):
        self.url = url.rstrip("/")
        self.recorder = recorder
        self.opener = build_opener(ProxyHandler({}))

    def request(self, path, task=None, phase="poll", log=True):
        started = time.monotonic()
        raw = None if task is None else json.dumps(task).encode("utf-8")
        headers = {} if task is None else {"Content-Type": "application/json"}
        status, result, error = 0, None, None
        try:
            request = Request(self.url + path, data=raw, headers=headers)
            try:
                response = self.opener.open(request, timeout=3)
            except HTTPError as exc:
                response = exc
            with response:
                status = response.code
                result = json.loads(response.read())
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
                recorder.event("integrity_error", task_id=task["task_id"], reason="result_or_process_count_mismatch")
                return False
            recorder.event("completed", task_id=task["task_id"],
                           latency_ms=round((time.monotonic() - started) * 1000, 3),
                           delivery_count=job.get("delivery_count"))
            return True
        time.sleep(0.25)
    check_slo()
    recorder.event("unresolved", task_id=task["task_id"], accepted_response_seen=accepted)
    return False


def probe(kind, host, port):
    started = time.monotonic()
    error = None
    try:
        if kind == "ping":
            completed = subprocess.run(
                ["ping", "-4", "-n", "-c", "1", "-W", "1", "-M", "do", "-s", "1372", host],
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


def main():
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
