"""Camada de mensageria sobre Amazon SQS (ou LocalStack em desenvolvimento).

Todas as mensagens usam o envelope ``{"type", "payload", "correlation_id"}``.
Filas ``.fifo`` recebem ``MessageGroupId`` (ordem por OS) e
``MessageDeduplicationId`` (idempotência no envio).
"""

import json
import logging
import os
import threading
from typing import Callable
from uuid import uuid4

import boto3

from app import observability

logger = logging.getLogger(__name__)

MESSAGING_ENABLED = os.getenv("MESSAGING_ENABLED", "false").lower() == "true"
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
SQS_ENDPOINT_URL = os.getenv("SQS_ENDPOINT_URL") or None
POLL_WAIT_SECONDS = int(os.getenv("SQS_POLL_WAIT_SECONDS", "10"))

_client = None


def get_client():
    global _client
    if _client is None:
        _client = boto3.client("sqs", region_name=AWS_REGION, endpoint_url=SQS_ENDPOINT_URL)
    return _client


def publish(
    queue_url: str,
    message_type: str,
    payload: dict,
    group_id: str,
    dedup_id: str | None = None,
    correlation_id: str | None = None,
) -> str:
    cid = correlation_id or observability.get_correlation_id()
    body = json.dumps(
        {
            "type": message_type,
            "payload": payload,
            "correlation_id": cid,
        },
        default=str,
    )
    kwargs = {}
    if queue_url.endswith(".fifo"):
        kwargs["MessageGroupId"] = group_id
        kwargs["MessageDeduplicationId"] = dedup_id or str(uuid4())
    response = get_client().send_message(QueueUrl=queue_url, MessageBody=body, **kwargs)
    observability.METRICS["events_published"][message_type] += 1
    return response["MessageId"]


def receive(queue_url: str, max_messages: int = 10, wait_seconds: int | None = None) -> list[dict]:
    response = get_client().receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=max_messages,
        WaitTimeSeconds=POLL_WAIT_SECONDS if wait_seconds is None else wait_seconds,
    )
    return response.get("Messages", [])


def delete(queue_url: str, receipt_handle: str) -> None:
    get_client().delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)


def process_batch(queue_url: str, handler: Callable[[str, dict], None], wait_seconds: int | None = None) -> int:
    """Consome um lote. Mensagem só é apagada se o handler concluir sem erro;
    caso contrário volta à fila após o visibility timeout e, ao exceder
    ``maxReceiveCount``, segue para a DLQ."""
    processed = 0
    for message in receive(queue_url, wait_seconds=wait_seconds):
        try:
            envelope = json.loads(message["Body"])
            observability.set_correlation_id(envelope.get("correlation_id"))
            handler(envelope["type"], envelope["payload"])
            observability.METRICS["events_consumed"][envelope["type"]] += 1
        except Exception:
            logger.exception("failed to process message %s from %s", message.get("MessageId"), queue_url)
            continue
        delete(queue_url, message["ReceiptHandle"])
        processed += 1
    return processed


def start_consumer(queue_url: str, handler: Callable[[str, dict], None]) -> threading.Event:
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                process_batch(queue_url, handler)
            except Exception:
                logger.exception("consumer loop error for %s", queue_url)
                stop.wait(5)

    threading.Thread(target=loop, name=f"consumer-{queue_url.rsplit('/', 1)[-1]}", daemon=True).start()
    return stop

