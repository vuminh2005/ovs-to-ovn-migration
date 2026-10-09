import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import runner

TASK = {"task_id": "b162d212-73bc-4427-81ae-702f70cf7d30", "run_id": "test-client",
        "client_id": "client", "payload": "test"}


def job(status="done", digest=None, count=1):
    return {"task_id": TASK["task_id"], "status": status,
            "result_sha256": digest or hashlib.sha256(b"test").hexdigest(), "process_count": count}


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.recorder = runner.Recorder(self.directory.name)

    def tearDown(self):
        if not self.recorder.stream.closed:
            self.recorder.stream.close()
        self.directory.cleanup()

    def test_percentiles_use_nearest_rank_and_handle_empty(self):
        self.assertIsNone(runner.percentile([], 99))
        self.assertEqual(runner.percentile(list(range(1, 21)), 95), 19)
        self.assertEqual(runner.percentile(list(range(1, 21)), 99), 20)

    def test_lost_submit_response_retries_same_id_and_payload(self):
        client = Mock()
        client.request.side_effect = [
            (0, None, "TimeoutError"), (200, {"created": False, "job": job()}, None)
        ]
        with patch.object(runner.time, "sleep"):
            self.assertTrue(runner.run_task(client, TASK, self.recorder, lambda: float("inf"), 10))
        calls = client.request.call_args_list
        self.assertEqual(calls[0].args, ("/jobs", TASK))
        self.assertEqual(calls[1].args, ("/jobs", TASK))
        self.assertEqual(self.recorder.counts["created"], 1)
        self.assertEqual(self.recorder.counts["accepted"], 1)
        self.assertEqual(self.recorder.counts["completed"], 1)

    def test_pending_job_is_polled_without_resubmission(self):
        client = Mock()
        client.request.side_effect = [(202, {"job": job("pending")}, None), (200, {"job": job()}, None)]
        with patch.object(runner.time, "sleep"):
            self.assertTrue(runner.run_task(client, TASK, self.recorder, lambda: float("inf"), 10))
        self.assertEqual(client.request.call_args_list[1].args, ("/jobs/" + TASK["task_id"], None))

    def test_wrong_digest_is_not_counted_as_completed(self):
        client = Mock()
        client.request.return_value = (200, {"job": job(digest="bad")}, None)
        self.assertFalse(runner.run_task(client, TASK, self.recorder, lambda: float("inf"), 10))
        self.assertEqual(self.recorder.counts["integrity_error"], 1)
        self.assertEqual(self.recorder.counts["completed"], 0)

    def test_duplicate_processing_is_integrity_error(self):
        client = Mock()
        client.request.return_value = (200, {"job": job(count=2)}, None)
        self.assertFalse(runner.run_task(client, TASK, self.recorder, lambda: float("inf"), 10))
        self.assertEqual(self.recorder.counts["integrity_error"], 1)

    def test_unresolved_is_not_labeled_lost(self):
        client = Mock()
        self.assertFalse(runner.run_task(client, TASK, self.recorder, lambda: 0, 10))
        client.request.assert_not_called()
        self.assertEqual(self.recorder.counts["unresolved"], 1)
        events = [json.loads(line) for line in (Path(self.directory.name) / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["kind"], "unresolved")
        self.assertFalse(events[-1]["accepted_response_seen"])

    def test_unexpected_http_409_stops_task(self):
        client = Mock()
        client.request.return_value = (409, {"error": "conflict"}, None)
        self.assertFalse(runner.run_task(client, TASK, self.recorder, lambda: float("inf"), 10))
        self.assertEqual(client.request.call_count, 1)

    def test_http_error_response_is_logged_and_returned(self):
        client = runner.Client("http://127.0.0.1", self.recorder)
        client.opener = Mock()
        client.opener.open.side_effect = HTTPError(
            "http://127.0.0.1/jobs", 503, "Unavailable", {}, io.BytesIO(b'{"error":"db"}')
        )
        code, result, error = client.request("/jobs", TASK, phase="submit")
        self.assertEqual((code, result, error), (503, {"error": "db"}, None))
        self.assertEqual(self.recorder.counts["http_failures"], 1)

    def test_network_error_is_status_zero(self):
        client = runner.Client("http://127.0.0.1", self.recorder)
        client.opener = Mock()
        client.opener.open.side_effect = TimeoutError()
        self.assertEqual(client.request("/jobs")[0], 0)
        self.assertEqual(self.recorder.counts["http_failures"], 1)

    def test_failure_window_closes_only_on_success(self):
        start = self.recorder.started
        with patch.object(runner.time, "monotonic", return_value=start + 2):
            self.recorder.event("ping", ok=False)
        with patch.object(runner.time, "monotonic", return_value=start + 3):
            self.recorder.event("ping", ok=False)
        with patch.object(runner.time, "monotonic", return_value=start + 5):
            self.recorder.event("ping", ok=True)
        window = self.recorder.probes["ping"]["failure_windows"][0]
        self.assertEqual(window["observed_seconds"], 3)
        self.assertTrue(window["recovery_observed"])

    def test_open_failure_window_has_no_invented_recovery(self):
        start = self.recorder.started
        with patch.object(runner.time, "monotonic", return_value=start + 2):
            self.recorder.event("tcp", ok=False)
        with patch.object(runner.time, "monotonic", return_value=start + 4):
            self.recorder.event("tcp", ok=False)
        with patch.object(runner.time, "monotonic", return_value=start + 10):
            summary = self.recorder.finish({}, None, "unavailable")
        window = summary["probes"]["tcp"]["failure_windows"][0]
        self.assertFalse(window["recovery_observed"])
        self.assertEqual(window["observed_seconds"], 2)

    def healthy_counts(self):
        self.recorder.event("created", task_id=TASK["task_id"])
        self.recorder.event("accepted", task_id=TASK["task_id"])
        self.recorder.event("completed", task_id=TASK["task_id"], latency_ms=100)
        self.recorder.event("ping", ok=True)
        self.recorder.event("tcp", ok=True)

    def test_server_counts_must_reconcile(self):
        self.healthy_counts()
        summary = self.recorder.finish({}, {"accepted": 2, "done": 2, "processed": 2, "pending": 0}, None)
        self.assertFalse(summary["baseline_passed"])

    def test_broker_redelivery_does_not_imply_duplicate_processing(self):
        self.healthy_counts()
        summary = self.recorder.finish({}, {"accepted": 1, "done": 1, "processed": 1,
                                           "pending": 0, "duplicate_deliveries": 2}, None)
        self.assertTrue(summary["baseline_passed"])
        self.assertTrue((Path(self.directory.name) / "summary.json").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
