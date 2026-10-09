"""Offline tests for HTTP behavior and commit/ACK failure boundaries."""
import io
import json
import sys
import types
import unittest
import uuid
from contextlib import contextmanager
from unittest.mock import Mock, patch

# Offline environment has no DB/broker libraries. Stub only those interfaces;
# HTTP routing, validation, worker callbacks and common.complete_job run as written.
sys.modules["pika"] = types.SimpleNamespace(BasicProperties=lambda **kw: types.SimpleNamespace(**kw))
sys.modules["psycopg2"] = types.ModuleType("psycopg2")
extras = types.ModuleType("psycopg2.extras")
extras.RealDictCursor = object
sys.modules["psycopg2.extras"] = extras

import api
import common
import worker

TASK_ID = str(uuid.uuid4())
TASK = {"task_id": TASK_ID, "run_id": "test", "client_id": "client", "payload": "test-data"}


def call_api(path, method="GET", data=None, headers=None, raw=None, query=""):
    if raw is None:
        raw = b"" if data is None else json.dumps(data).encode()
    environ = {"PATH_INFO": path, "REQUEST_METHOD": method, "QUERY_STRING": query,
               "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": str(len(raw)),
               "wsgi.input": io.BytesIO(raw)}
    environ.update(headers or {})
    response = []
    body = b"".join(api.application(environ, lambda status, fields: response.append((status, fields))))
    status, headers_out = response[0]
    assert int(dict(headers_out)["Content-Length"]) == len(body)
    return int(status.split()[0]), json.loads(body)


class HttpTests(unittest.TestCase):
    def test_live_does_not_need_dependencies(self):
        self.assertEqual(call_api("/live")[0], 200)

    def test_health_reports_broker_outage(self):
        with patch.object(common, "dependency_health", return_value={"postgresql": "ok", "rabbitmq": "timeout"}):
            code, body = call_api("/health")
            self.assertEqual(code, 503)
            self.assertEqual(body["status"], "degraded")

    def test_submit_pending_returns_accepted(self):
        with patch.object(common, "submit_job", return_value=({"task_id": TASK_ID, "status": "pending"}, True)) as submit:
            code, body = call_api("/jobs", "POST", TASK)
            self.assertEqual(code, 202)
            self.assertTrue(body["created"])
            self.assertEqual(submit.call_args.args[0], TASK)

    def test_repeat_completed_job_returns_existing(self):
        with patch.object(common, "submit_job", return_value=({"status": "done", "process_count": 1}, False)):
            code, body = call_api("/jobs", "POST", TASK)
            self.assertEqual(code, 200)
            self.assertFalse(body["created"])

    def test_input_conflict_returns_409(self):
        with patch.object(common, "submit_job", side_effect=common.Conflict):
            self.assertEqual(call_api("/jobs", "POST", TASK)[0], 409)

    def test_database_failure_returns_retryable_503(self):
        with patch.object(common, "submit_job", side_effect=RuntimeError("private detail")):
            code, body = call_api("/jobs", "POST", TASK)
            self.assertEqual(code, 503)
            self.assertTrue(body["retryable"])
            self.assertNotIn("private detail", json.dumps(body))

    def test_invalid_uuid_rejected_before_database(self):
        with patch.object(common, "submit_job") as submit:
            self.assertEqual(call_api("/jobs", "POST", {**TASK, "task_id": "bad"})[0], 400)
            submit.assert_not_called()

    def test_invalid_json_returns_400(self):
        self.assertEqual(call_api("/jobs", "POST", raw=b"{broken")[0], 400)

    def test_array_json_returns_400(self):
        self.assertEqual(call_api("/jobs", "POST", [TASK])[0], 400)

    def test_wrong_content_type_returns_415(self):
        self.assertEqual(call_api("/jobs", "POST", TASK, {"CONTENT_TYPE": "text/plain"})[0], 415)

    def test_oversize_body_returns_413_before_read(self):
        self.assertEqual(call_api("/jobs", "POST", TASK, {"CONTENT_LENGTH": str(api.MAX_BODY + 1)})[0], 413)

    def test_utf8_payload_limit_counts_bytes(self):
        with self.assertRaises(ValueError):
            api.validate_task({**TASK, "payload": "é" * 32769})

    def test_missing_job_returns_404(self):
        with patch.object(common, "get_job", return_value=None):
            self.assertEqual(call_api("/jobs/" + TASK_ID)[0], 404)

    def test_statistics_are_scoped_to_run(self):
        with patch.object(common, "statistics", return_value={"accepted": 1}) as stats:
            self.assertEqual(call_api("/stats", query="run_id=run-123")[0], 200)
            stats.assert_called_once_with("run-123")


class FakeCursor:
    def __init__(self, row):
        self.row = row
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False
    def execute(self, *_):
        pass
    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, row):
        self.row = row
    def cursor(self, **_):
        return FakeCursor(self.row)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.channel = Mock()
        self.method = types.SimpleNamespace(delivery_tag=7, redelivered=False)
        self.body = json.dumps({"task_id": TASK_ID}).encode()

    def fake_database(self, status="pending", commit_fails=False, missing=False):
        events = self.events
        @contextmanager
        def context():
            events.append("begin")
            row = None if missing else {"status": status, "payload": "test-data"}
            yield FakeConnection(row)
            events.append("commit")
            if commit_fails:
                raise RuntimeError("commit outcome unknown")
        return context

    def test_ack_is_after_commit(self):
        self.events = []
        self.channel.basic_ack.side_effect = lambda **_: self.events.append("ack")
        with patch.object(common, "database", self.fake_database()), patch.object(worker, "event"):
            worker.consume(self.channel, self.method, None, self.body)
        self.assertEqual(self.events, ["begin", "commit", "ack"])

    def test_commit_failure_never_acks_or_discards_message(self):
        self.events = []
        with patch.object(common, "database", self.fake_database(commit_fails=True)):
            with self.assertRaises(RuntimeError):
                worker.consume(self.channel, self.method, None, self.body)
        self.channel.basic_ack.assert_not_called()
        self.channel.basic_nack.assert_not_called()

    def test_database_connection_failure_never_acks(self):
        with patch.object(common, "complete_job", side_effect=OSError("network down")):
            with self.assertRaises(OSError):
                worker.consume(self.channel, self.method, None, self.body)
        self.channel.basic_ack.assert_not_called()

    def test_completed_delivery_is_acked_as_duplicate(self):
        self.events = []
        with patch.object(common, "database", self.fake_database(status="done")), patch.object(worker, "event") as event:
            worker.consume(self.channel, self.method, None, self.body)
        self.channel.basic_ack.assert_called_once_with(delivery_tag=7)
        self.assertTrue(event.call_args.kwargs["duplicate"])

    def test_unknown_job_is_rejected(self):
        self.events = []
        with patch.object(common, "database", self.fake_database(missing=True)), patch.object(worker, "event"):
            worker.consume(self.channel, self.method, None, self.body)
        self.channel.basic_nack.assert_called_once_with(delivery_tag=7, requeue=False)

    def test_malformed_message_is_rejected_without_db_call(self):
        with patch.object(common, "complete_job") as complete, patch.object(worker, "event"):
            worker.consume(self.channel, self.method, None, b"not-json")
        complete.assert_not_called()
        self.channel.basic_nack.assert_called_once_with(delivery_tag=7, requeue=False)

    def test_published_message_is_persistent_and_mandatory(self):
        with patch.object(common, "pending_jobs", return_value=[TASK_ID]), patch.object(worker, "event"):
            worker.publish_pending(self.channel, Mock())
        options = self.channel.basic_publish.call_args.kwargs
        self.assertEqual(options["properties"].delivery_mode, 2)
        self.assertTrue(options["mandatory"])
        self.assertEqual(json.loads(options["body"])["task_id"], TASK_ID)

    def test_failed_publish_does_not_claim_success(self):
        self.channel.basic_publish.side_effect = OSError("connection lost")
        with patch.object(common, "pending_jobs", return_value=[TASK_ID]), patch.object(worker, "event") as event:
            with self.assertRaises(OSError):
                worker.publish_pending(self.channel, Mock())
        event.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
