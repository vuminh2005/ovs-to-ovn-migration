"""Shared PostgreSQL outbox and AMQP configuration for the EW lab."""
import hashlib
import os
from contextlib import contextmanager

import pika
import psycopg2
from psycopg2.extras import RealDictCursor

EXCHANGE = "ew.tasks"
QUEUE = "ew.jobs"
ROUTING_KEY = "jobs"


class Conflict(Exception):
    pass


class UnknownJob(Exception):
    pass


@contextmanager
def database():
    connection = psycopg2.connect(
        host=os.environ["EW_DB_HOST"], port=5432, dbname="ewlab", user="ewapp",
        password=os.environ["EW_DB_PASSWORD"], connect_timeout=3,
        options="-c statement_timeout=3000 -c lock_timeout=3000",
        keepalives=1, keepalives_idle=5, keepalives_interval=2, keepalives_count=3,
        application_name="ew-workload",
    )
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def broker():
    return pika.BlockingConnection(pika.ConnectionParameters(
        host=os.environ["EW_MQ_HOST"], port=5672, virtual_host="ewlab",
        credentials=pika.PlainCredentials("ewapp", os.environ["EW_MQ_PASSWORD"]),
        heartbeat=10, connection_attempts=1, socket_timeout=3,
        stack_timeout=8, blocked_connection_timeout=5,
    ))


def topology(channel):
    channel.exchange_declare(exchange=EXCHANGE, exchange_type="direct", durable=True)
    channel.queue_declare(queue=QUEUE, durable=True)
    channel.queue_bind(queue=QUEUE, exchange=EXCHANGE, routing_key=ROUTING_KEY)


def initialize():
    with database() as connection, connection.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ew_jobs (
                task_id uuid PRIMARY KEY,
                run_id text NOT NULL,
                client_id text NOT NULL,
                payload text NOT NULL,
                status text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'done')),
                result_sha256 text,
                process_count integer NOT NULL DEFAULT 0,
                delivery_count integer NOT NULL DEFAULT 0,
                created_at timestamptz NOT NULL DEFAULT now(),
                completed_at timestamptz
            );
            CREATE INDEX IF NOT EXISTS ew_jobs_pending_idx
                ON ew_jobs (created_at) WHERE status = 'pending';
            CREATE INDEX IF NOT EXISTS ew_jobs_run_idx ON ew_jobs (run_id);
        """)


def public_job(row):
    fields = ("task_id", "run_id", "client_id", "status", "result_sha256",
              "process_count", "delivery_count", "created_at", "completed_at")
    result = {field: row[field] for field in fields}
    result["task_id"] = str(result["task_id"])
    for field in ("created_at", "completed_at"):
        if result[field] is not None:
            result[field] = result[field].isoformat()
    return result


def submit_job(task):
    with database() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute("""
            INSERT INTO ew_jobs (task_id, run_id, client_id, payload)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (task_id) DO NOTHING
            RETURNING task_id
        """, (task["task_id"], task["run_id"], task["client_id"], task["payload"]))
        created = cursor.fetchone() is not None
        cursor.execute("SELECT * FROM ew_jobs WHERE task_id = %s", (task["task_id"],))
        row = cursor.fetchone()
        if any(row[field] != task[field] for field in ("run_id", "client_id", "payload")):
            raise Conflict("task_id already belongs to different input")
        result = public_job(row)
    # This point is reached only after a successful PostgreSQL commit.
    return result, created


def get_job(task_id):
    with database() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute("SELECT * FROM ew_jobs WHERE task_id = %s", (task_id,))
        row = cursor.fetchone()
        return public_job(row) if row else None


def pending_jobs():
    with database() as connection, connection.cursor() as cursor:
        cursor.execute("""
            SELECT task_id FROM ew_jobs WHERE status = 'pending'
            ORDER BY created_at LIMIT 32
        """)
        return [str(row[0]) for row in cursor.fetchall()]


def complete_job(task_id):
    with database() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute("SELECT * FROM ew_jobs WHERE task_id = %s FOR UPDATE", (task_id,))
        row = cursor.fetchone()
        if row is None:
            raise UnknownJob(task_id)
        duplicate = row["status"] == "done"
        digest = hashlib.sha256(row["payload"].encode("utf-8")).hexdigest()
        cursor.execute("""
            UPDATE ew_jobs SET
                status = 'done', result_sha256 = %s,
                process_count = process_count + CASE WHEN status = 'pending' THEN 1 ELSE 0 END,
                delivery_count = delivery_count + 1,
                completed_at = COALESCE(completed_at, now())
            WHERE task_id = %s
        """, (digest, task_id))
    # Caller must ACK only after this function returns: commit can itself fail.
    return duplicate


def statistics(run_id=None):
    with database() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute("""
            SELECT count(*) AS accepted,
                count(*) FILTER (WHERE status = 'pending') AS pending,
                count(*) FILTER (WHERE status = 'done') AS done,
                coalesce(sum(process_count), 0) AS processed,
                coalesce(sum(greatest(delivery_count - 1, 0)), 0) AS duplicate_deliveries
            FROM ew_jobs WHERE (%s::text IS NULL OR run_id = %s)
        """, (run_id, run_id))
        return {key: int(value) for key, value in cursor.fetchone().items()}


def dependency_health():
    result = {}
    try:
        with database() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        result["postgresql"] = "ok"
    except Exception as exc:
        result["postgresql"] = type(exc).__name__
    try:
        connection = broker()
        connection.close()
        result["rabbitmq"] = "ok"
    except Exception as exc:
        result["rabbitmq"] = type(exc).__name__
    return result


if __name__ == "__main__":
    initialize()
    print("EW_DATABASE_SCHEMA_OK")
