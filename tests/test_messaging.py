import boto3
import pytest
from moto import mock_aws
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main, messaging


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    main.Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        yield session
    engine.dispose()


@pytest.fixture
def sqs(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
        monkeypatch.setattr(messaging, "MESSAGING_ENABLED", True)
        monkeypatch.setattr(messaging, "_client", None)
        client = boto3.client("sqs", region_name="us-east-1")
        events_url = client.create_queue(
            QueueName="os-events.fifo",
            Attributes={"FifoQueue": "true"},
        )["QueueUrl"]
        commands_url = client.create_queue(
            QueueName="inventory-commands.fifo",
            Attributes={"FifoQueue": "true"},
        )["QueueUrl"]
        monkeypatch.setattr(main, "OS_EVENTS_QUEUE_URL", events_url)
        monkeypatch.setattr(main, "INVENTORY_COMMANDS_QUEUE_URL", commands_url)
        yield {"events": events_url, "commands": commands_url}
        monkeypatch.setattr(messaging, "_client", None)


def test_reserve_and_release_stock_commands(db, sqs):
    db.add(main.Part(sku="oil", name="Oil", unit_price=10, available_stock=5, active=True))
    db.commit()

    main.handle_inventory_command(db, "ReserveStock", {"order_id": "o1", "items": [{"sku": "oil", "quantity": 2}]})
    assert db.get(main.Part, "oil").available_stock == 3
    events = []
    messaging.process_batch(sqs["events"], lambda t, p: events.append((t, p)), wait_seconds=0)
    assert events == [("StockReserved", {"order_id": "o1", "items": [{"sku": "oil", "quantity": 2}]})]

    main.handle_inventory_command(db, "ReleaseStock", {"order_id": "o1"})
    assert db.get(main.Part, "oil").available_stock == 5
    events.clear()
    messaging.process_batch(sqs["events"], lambda t, p: events.append((t, p)), wait_seconds=0)
    assert events == [("StockReleased", {"order_id": "o1"})]


def test_reserve_stock_command_emits_failure_when_insufficient(db, sqs):
    db.add(main.Part(sku="oil", name="Oil", unit_price=10, available_stock=1, active=True))
    db.commit()

    main.handle_inventory_command(db, "ReserveStock", {"order_id": "o2", "items": [{"sku": "oil", "quantity": 3}]})
    assert db.get(main.Part, "oil").available_stock == 1
    events = []
    messaging.process_batch(sqs["events"], lambda t, p: events.append((t, p)), wait_seconds=0)
    assert len(events) == 1
    assert events[0][0] == "StockReservationFailed"
    assert "insufficient stock" in events[0][1]["reason"]

