"""Live E2E check; uses only Python's standard library on each client VM."""
import argparse
import hashlib
import json
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://192.168.101.11:8080")
    parser.add_argument("--client-id", required=True)
    args = parser.parse_args()
    opener = build_opener(ProxyHandler({}))

    def request(path, data=None):
        raw = None if data is None else json.dumps(data).encode("utf-8")
        headers = {} if data is None else {"Content-Type": "application/json"}
        req = Request(args.url.rstrip("/") + path, data=raw, headers=headers)
        try:
            response = opener.open(req, timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            return response.code, json.loads(response.read())

    code, health = request("/health")
    if code != 200:
        raise RuntimeError(f"Dependencies not ready: HTTP {code}, {health}")
    print("DEPENDENCIES_HEALTH_OK", flush=True)

    task = {
        "task_id": str(uuid.uuid4()),
        "run_id": "probe-" + uuid.uuid4().hex,
        "client_id": args.client_id,
        "payload": ("ew-network-check:" * 512),
    }
    expected = hashlib.sha256(task["payload"].encode("utf-8")).hexdigest()
    started = time.monotonic()
    code, result = request("/jobs", task)
    if code not in (200, 202) or result.get("job", {}).get("task_id") != task["task_id"]:
        raise RuntimeError(f"Submit failed: HTTP {code}, {result}")

    deadline = time.monotonic() + 30
    while True:
        code, result = request("/jobs/" + task["task_id"])
        if code != 200:
            raise RuntimeError(f"Poll failed: HTTP {code}, {result}")
        job = result["job"]
        if job["status"] == "done":
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Task remains pending: {task['task_id']}")
        time.sleep(0.2)
    elapsed_ms = round((time.monotonic() - started) * 1000, 2)
    if job["result_sha256"] != expected or job["process_count"] != 1:
        raise RuntimeError(f"Result mismatch: {job}")
    print(f"E2E_RESULT_OK client={args.client_id} task_id={task['task_id']} latency_ms={elapsed_ms}")

    # Retry with the same ID and input: must reuse the existing result.
    code, result = request("/jobs", task)
    if code != 200 or result.get("created") is not False or result["job"]["process_count"] != 1:
        raise RuntimeError(f"Idempotency failed: HTTP {code}, {result}")
    print("HTTP_RETRY_SAME_TASK_OK")

    code, result = request("/jobs", {**task, "payload": "different-input"})
    if code != 409:
        raise RuntimeError(f"Input conflict not rejected: HTTP {code}, {result}")
    print("TASK_ID_CONFLICT_CHECK_OK")

    code, stats = request("/stats?" + urlencode({"run_id": task["run_id"]}))
    if code != 200 or (stats["accepted"], stats["pending"], stats["done"], stats["processed"]) != (1, 0, 1, 1):
        raise RuntimeError(f"Stats mismatch: HTTP {code}, {stats}")
    print("RUN_STATS:", json.dumps(stats, sort_keys=True))
    print("WORKLOAD_E2E_OK", flush=True)


if __name__ == "__main__":
    main()
