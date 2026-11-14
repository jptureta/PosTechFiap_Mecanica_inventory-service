from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import Actor, get_actor
from app import main


def client_for_test(monkeypatch=None):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    main.Base.metadata.create_all(engine)
    main.engine = engine
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    if monkeypatch is not None:
        monkeypatch.setattr(main, "SessionLocal", factory)
    main.INTERNAL_TOKEN = "test-internal"

    def override_db():
        with factory() as session:
            yield session

    main.app.dependency_overrides[main.get_db] = override_db
    main.app.dependency_overrides[get_actor] = lambda: Actor(is_admin=True, customer_id=None)
    return TestClient(main.app), engine


def test_reservation_is_idempotent_and_release_restores_stock():
    client, engine = client_for_test()
    headers = {"X-Internal-Token": "test-internal"}
    try:
        assert client.put(
            "/parts/oil",
            json={"name": "Oil", "unit_price": "10.00", "available_stock": 3},
            headers=headers,
        ).status_code == 200
        payload = {"order_id": "order-1", "items": [{"sku": "oil", "quantity": 2}]}
        first = client.post("/reservations", json=payload, headers=headers)
        second = client.post("/reservations", json=payload, headers=headers)
        assert first.status_code == second.status_code == 200
        assert client.get("/parts/oil").json()["available_stock"] == 1
        assert client.delete("/reservations/order-1", headers=headers).json()["status"] == "RELEASED"
        assert client.get("/parts/oil").json()["available_stock"] == 3
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()


def test_reservation_rejects_insufficient_stock():
    client, engine = client_for_test()
    headers = {"X-Internal-Token": "test-internal"}
    try:
        client.put(
            "/parts/filter",
            json={"name": "Filter", "unit_price": "9.00", "available_stock": 1},
            headers=headers,
        )
        response = client.post(
            "/reservations",
            json={"order_id": "order-2", "items": [{"sku": "filter", "quantity": 2}]},
            headers=headers,
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "insufficient stock: filter"
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()


def test_inventory_commands_publish_reservation_outcomes(monkeypatch):
    client, engine = client_for_test(monkeypatch)
    events = []
    monkeypatch.setattr(main.messaging, "publish", lambda queue, kind, payload: events.append((queue, kind, payload)))
    headers = {"X-Internal-Token": "test-internal"}
    try:
        client.put(
            "/parts/oil",
            json={"name": "Oil", "unit_price": "10.00", "available_stock": 1},
            headers=headers,
        )
        main.handle_inventory_command("ReserveStock", {"order_id": "order-reserved", "items": [{"sku": "oil", "quantity": 1}]})
        main.handle_inventory_command("ReserveStock", {"order_id": "order-failed", "items": [{"sku": "oil", "quantity": 2}]})
        main.handle_inventory_command("ReleaseStock", {"order_id": "order-reserved"})

        assert [event[1] for event in events] == [
            "StockReserved",
            "StockReservationFailed",
            "StockReleased",
        ]
        assert events[1][2]["reason"] == "insufficient stock: oil"
        assert client.get("/parts/oil").json()["available_stock"] == 1
        assert all(event[0] == main.messaging.OS_EVENTS_QUEUE for event in events)
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()
