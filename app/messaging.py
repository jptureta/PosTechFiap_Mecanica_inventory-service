"""RabbitMQ transport for inventory commands and order events."""

import json
import logging
import os
import threading
from typing import Callable
from uuid import uuid4

import pika

from app import observability

logger = logging.getLogger(__name__)
MESSAGING_ENABLED = os.getenv("MESSAGING_ENABLED", "false").lower() == "true"
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "")
OS_EVENTS_QUEUE = os.getenv("OS_EVENTS_QUEUE", "oficina.os.events")
INVENTORY_COMMANDS_QUEUE = os.getenv("INVENTORY_COMMANDS_QUEUE", "oficina.inventory.commands")


def _connection() -> pika.BlockingConnection:
    if not RABBITMQ_URL:
        raise RuntimeError("RABBITMQ_URL must be configured when messaging is enabled")
    return pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))


def _declare_queue(channel: pika.adapters.blocking_connection.BlockingChannel, queue_name: str) -> None:
    dead_letter_queue = f"{queue_name}.dlq"
    channel.queue_declare(queue=dead_letter_queue, durable=True)
    channel.queue_declare(
        queue=queue_name,
        durable=True,
        # Sem x-dead-letter-exchange o RabbitMQ descarta a mensagem rejeitada.
        # "" = exchange padrão, que entrega direto na fila com o nome da routing key.
        arguments={"x-dead-letter-exchange": "", "x-dead-letter-routing-key": dead_letter_queue},
    )


def publish(
    queue_name: str,
    message_type: str,
    payload: dict,
    correlation_id: str | None = None,
) -> str:
    cid = correlation_id or observability.get_correlation_id()
    body = json.dumps(
        {"type": message_type, "payload": payload, "correlation_id": cid},
        default=str,
    )
    connection = _connection()
    try:
        channel = connection.channel()
        _declare_queue(channel, queue_name)
        channel.confirm_delivery()
        message_id = str(uuid4())
        confirmed = channel.basic_publish(
            exchange="",
            routing_key=queue_name,
            body=body,
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,
                message_id=message_id,
                correlation_id=cid,
            ),
            mandatory=True,
        )
        if not confirmed:
            raise RuntimeError(f"RabbitMQ did not confirm delivery to {queue_name}")
    finally:
        if connection.is_open:
            connection.close()
    observability.METRICS["events_published"][message_type] += 1
    return message_id


def process_batch(queue_name: str, handler: Callable[[str, dict], None], max_messages: int = 10) -> int:
    connection = _connection()
    processed = 0
    try:
        channel = connection.channel()
        _declare_queue(channel, queue_name)
        channel.basic_qos(prefetch_count=1)
        for _ in range(max_messages):
            method, _, body = channel.basic_get(queue=queue_name, auto_ack=False)
            if method is None:
                break
            try:
                envelope = json.loads(body)
                message_type = envelope["type"]
                payload = envelope["payload"]
                if not isinstance(payload, dict):
                    raise ValueError("message payload must be an object")
                observability.set_correlation_id(envelope.get("correlation_id"))
                handler(message_type, payload)
            except Exception:
                logger.exception("failed to process RabbitMQ message from %s", queue_name)
                channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                continue
            channel.basic_ack(delivery_tag=method.delivery_tag)
            observability.METRICS["events_consumed"][message_type] += 1
            processed += 1
    finally:
        if connection.is_open:
            connection.close()
    return processed


def start_consumer(queue_name: str, handler: Callable[[str, dict], None]) -> threading.Event:
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                if process_batch(queue_name, handler) == 0:
                    stop.wait(1)
            except Exception:
                logger.exception("consumer loop error for %s", queue_name)
                stop.wait(5)

    threading.Thread(
        target=loop,
        name=f"consumer-{queue_name.rsplit('.', 1)[-1]}",
        daemon=True,
    ).start()
    return stop
