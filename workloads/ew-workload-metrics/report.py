"""Combine collected per-client summaries; no third-party dependencies."""
import argparse
import json
from pathlib import Path

CLIENTS = ("ew-client-a1", "ew-client-a2", "ew-client-b")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    args = parser.parse_args()
    directory = Path(args.directory)
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
