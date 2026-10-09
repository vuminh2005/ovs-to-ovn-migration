"""Republish pending DB jobs and consume with commit-before-ACK semantics."""
import json
import signal
import time
import uuid

import pika
import common

RUNNING = True


def event(name, **fields):
    print(json.dumps({"event": name, **fields}, separators=(",", ":")), flush=True)


def stop(_signum, _frame):
    global RUNNING
    RUNNING = False


def consume(channel, method, _properties, body):
    try:
        value = json.loads(body)
        if not isinstance(value, dict) or not isinstance(value.get("task_id"), str):
            raise ValueError("invalid task_id")
        task_id = str(uuid.UUID(value["task_id"]))
    except (ValueError, TypeError, UnicodeError):
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        event("invalid_message_rejected")
        return
    try:
        duplicate = common.complete_job(task_id)
    except common.UnknownJob:
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        event("unknown_task_rejected", task_id=task_id)
        return
    # All other errors propagate. Main loop closes the connection; unacked
    # messages are requeued, including a commit whose outcome is uncertain.
    channel.basic_ack(delivery_tag=method.delivery_tag)
    event("job_acked", task_id=task_id, duplicate=duplicate,
          redelivered=bool(method.redelivered))


def publish_pending(channel, connection):
    for task_id in common.pending_jobs():
        if not RUNNING:
            return
        channel.basic_publish(
            exchange=common.EXCHANGE, routing_key=common.ROUTING_KEY,
            body=json.dumps({"task_id": task_id}).encode("utf-8"),
            properties=pika.BasicProperties(delivery_mode=2, content_type="application/json",
                                            message_id=task_id),
            mandatory=True,
        )
        event("job_publish_confirmed", task_id=task_id)
        connection.process_data_events(time_limit=0)


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while RUNNING:
        connection = None
        try:
            connection = common.broker()
            channel = connection.channel()
            common.topology(channel)
            channel.basic_qos(prefetch_count=8)
            channel.confirm_delivery()
            channel.basic_consume(queue=common.QUEUE, on_message_callback=consume, auto_ack=False)
            event("worker_connected")
            next_scan = 0.0
            while RUNNING and connection.is_open:
                connection.process_data_events(time_limit=0.2)
                if time.monotonic() >= next_scan:
                    publish_pending(channel, connection)
                    next_scan = time.monotonic() + 2.0
        except Exception as exc:
            event("worker_retry", error_type=type(exc).__name__)
        finally:
            if connection is not None and connection.is_open:
                try:
                    connection.close()
                except Exception:
                    pass
        if RUNNING:
            time.sleep(1)
    event("worker_stopped")


if __name__ == "__main__":
    main()
