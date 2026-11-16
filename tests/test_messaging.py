import json
import threading
from types import SimpleNamespace

import pytest

from app import messaging


class FakeRabbit:
    def __init__(self):
        self.queues = {}
        self.next_delivery_tag = 1

    def connection(self):
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, broker):
        self.broker = broker
        self.is_open = True

    def channel(self):
        return FakeChannel(self.broker)

    def close(self):
        self.is_open = False


class FakeChannel:
    def __init__(self, broker):
        self.broker = broker
        self.inflight = {}

    def queue_declare(self, queue, **_):
        self.broker.queues.setdefault(queue, [])

    def confirm_delivery(self):
        return None

    def basic_publish(self, routing_key, body, **_):
        self.broker.queues.setdefault(routing_key, []).append(body)
        return True

    def basic_qos(self, **_):
        return None

    def basic_get(self, queue, **_):
        messages = self.broker.queues.setdefault(queue, [])
        if not messages:
            return None, None, None
        tag = self.broker.next_delivery_tag
        self.broker.next_delivery_tag += 1
        body = messages[0]
        self.inflight[tag] = (queue, body)
        return SimpleNamespace(delivery_tag=tag), None, body

    def basic_ack(self, delivery_tag):
        queue, body = self.inflight.pop(delivery_tag)
        self.broker.queues[queue].remove(body)

    def basic_nack(self, delivery_tag, requeue):
        queue, body = self.inflight.pop(delivery_tag)
        self.broker.queues[queue].remove(body)
        if requeue:
            self.broker.queues[queue].append(body)
        else:
            self.broker.queues.setdefault(f"{queue}.dlq", []).append(body)


def test_publish_and_process_batch_acknowledges_message(monkeypatch):
    broker = FakeRabbit()
    monkeypatch.setattr(messaging, "_connection", broker.connection)
    message_id = messaging.publish(
        "inventory.commands", "ReserveStock", {"order_id": "order-1"}, correlation_id="corr-from-os"
    )
    envelope = json.loads(broker.queues["inventory.commands"][0])

    assert message_id
    # O ID de rastreio recebido é repassado, não recriado a cada mensagem.
    assert envelope == {
        "type": "ReserveStock",
        "payload": {"order_id": "order-1"},
        "correlation_id": "corr-from-os",
    }
    received = []
    assert messaging.process_batch(
        "inventory.commands",
        lambda event_type, payload: received.append((event_type, payload)),
    ) == 1
    assert received == [("ReserveStock", {"order_id": "order-1"})]
    assert broker.queues["inventory.commands"] == []


def test_process_batch_dead_letters_failed_handler(monkeypatch):
    broker = FakeRabbit()
    monkeypatch.setattr(messaging, "_connection", broker.connection)
    messaging.publish("inventory.commands", "ReserveStock", {"order_id": "order-1"})

    def fail_handler(*_):
        raise RuntimeError("invalid command")

    assert messaging.process_batch("inventory.commands", fail_handler) == 0
    assert broker.queues["inventory.commands"] == []
    assert len(broker.queues["inventory.commands.dlq"]) == 1


def test_connection_requires_broker_url(monkeypatch):
    monkeypatch.setattr(messaging, "RABBITMQ_URL", "")

    with pytest.raises(RuntimeError, match="RABBITMQ_URL"):
        messaging._connection()


def test_start_consumer_stops_after_empty_batch(monkeypatch):
    processed = threading.Event()

    def empty_batch(*_):
        processed.set()
        return 0

    monkeypatch.setattr(messaging, "process_batch", empty_batch)
    stop = messaging.start_consumer("inventory.commands", lambda *_: None)
    try:
        assert processed.wait(timeout=2)
    finally:
        stop.set()


def test_consumed_correlation_id_is_reused_when_publishing(monkeypatch):
    broker = FakeRabbit()
    monkeypatch.setattr(messaging, "_connection", broker.connection)
    messaging.publish("inventory.commands", "ReserveStock", {"order_id": "o"}, correlation_id="corr-saga-1")

    def handler(*_):
        messaging.publish(messaging.OS_EVENTS_QUEUE, "StockReserved", {"order_id": "o"})

    assert messaging.process_batch("inventory.commands", handler) == 1
    event = json.loads(broker.queues[messaging.OS_EVENTS_QUEUE][0])
    assert event["correlation_id"] == "corr-saga-1"


def test_queue_declares_dead_letter_exchange():
    declared = {}

    class Channel:
        def queue_declare(self, queue, durable, arguments=None):
            declared[queue] = arguments

    messaging._declare_queue(Channel(), "inventory.commands")
    assert declared["inventory.commands"] == {
        "x-dead-letter-exchange": "",
        "x-dead-letter-routing-key": "inventory.commands.dlq",
    }
