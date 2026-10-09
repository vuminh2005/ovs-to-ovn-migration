"""Small JSON WSGI API, served by Gunicorn. No network I/O at import time."""
import json
import logging
import socket
import uuid
from http import HTTPStatus
from urllib.parse import parse_qs

import common

MAX_BODY = 131072
MAX_PAYLOAD = 65536
LOGGER = logging.getLogger("ew-api")


def validate_task(data):
    if not isinstance(data, dict):
        raise ValueError("JSON object required")
    task = {key: data.get(key) for key in ("task_id", "run_id", "client_id", "payload")}
    if not isinstance(task["task_id"], str):
        raise ValueError("task_id must be a UUID string")
    task["task_id"] = str(uuid.UUID(task["task_id"]))
    for field in ("run_id", "client_id"):
        value = task[field]
        if not isinstance(value, str) or not 1 <= len(value) <= 64:
            raise ValueError(field + " must contain 1 to 64 characters")
    if not isinstance(task["payload"], str):
        raise ValueError("payload must be a string")
    if len(task["payload"].encode("utf-8")) > MAX_PAYLOAD:
        raise ValueError("payload exceeds 65536 UTF-8 bytes")
    return task


def respond(start_response, code, data):
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    start_response(f"{code} {HTTPStatus(code).phrase}", [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
    ])
    return [body]


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")
    try:
        if path == "/live" and method == "GET":
            return respond(start_response, 200, {"status": "up", "host": socket.gethostname()})
        if path == "/health" and method == "GET":
            dependencies = common.dependency_health()
            healthy = all(value == "ok" for value in dependencies.values())
            return respond(start_response, 200 if healthy else 503,
                           {"status": "ok" if healthy else "degraded", "dependencies": dependencies})
        if path == "/jobs" and method == "POST":
            if environ.get("CONTENT_TYPE", "").split(";")[0].strip() != "application/json":
                return respond(start_response, 415, {"error": "application/json required"})
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
            except ValueError:
                return respond(start_response, 400, {"error": "invalid Content-Length"})
            if length > MAX_BODY:
                return respond(start_response, 413, {"error": "request too large"})
            if length <= 0:
                return respond(start_response, 400, {"error": "JSON body required"})
            try:
                task = validate_task(json.loads(environ["wsgi.input"].read(length)))
            except (ValueError, UnicodeError):
                return respond(start_response, 400, {"error": "invalid task input"})
            try:
                job, created = common.submit_job(task)
            except common.Conflict:
                return respond(start_response, 409, {"error": "task_id input conflict"})
            return respond(start_response, 202 if job["status"] == "pending" else 200,
                           {"created": created, "job": job})
        if path.startswith("/jobs/") and method == "GET":
            try:
                task_id = str(uuid.UUID(path[len("/jobs/"):]))
            except ValueError:
                return respond(start_response, 400, {"error": "invalid task_id"})
            job = common.get_job(task_id)
            return respond(start_response, 200 if job else 404,
                           {"job": job} if job else {"error": "task not found"})
        if path == "/stats" and method == "GET":
            values = parse_qs(environ.get("QUERY_STRING", ""))
            run_id = values.get("run_id", [None])[0]
            if run_id is not None and len(run_id) > 64:
                return respond(start_response, 400, {"error": "run_id too long"})
            return respond(start_response, 200, common.statistics(run_id))
        return respond(start_response, 404, {"error": "route not found"})
    except Exception as exc:
        # Return a retryable error without exposing credentials or SQL text.
        LOGGER.error("request_failed type=%s", type(exc).__name__)
        return respond(start_response, 503, {"error": "dependency_unavailable", "retryable": True})
