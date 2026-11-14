from fastapi.testclient import TestClient
import pytest
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
    if monkeypatch is not None:
        monkeypatch.setattr(main, "engine", engine)
        monkeypatch.setattr(main, "SessionLocal", factory)
        monkeypatch.setattr(main, "INTERNAL_TOKEN", "test-internal")

    def override_db():
        with factory() as session:
            yield session

    main.app.dependency_overrides[main.get_db] = override_db
    main.app.dependency_overrides[get_actor] = lambda: Actor(is_admin=True, customer_id=None)
    return TestClient(main.app), engine


def test_reservation_is_idempotent_and_release_restores_stock(monkeypatch):
    client, engine = client_for_test(monkeypatch)
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


def test_reservation_rejects_insufficient_stock(monkeypatch):
    client, engine = client_for_test(monkeypatch)
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


def test_catalog_authentication_and_upserts(monkeypatch):
    client, engine = client_for_test(monkeypatch)
    headers = {"X-Internal-Token": "test-internal"}
    try:
        assert client.get("/health").status_code == 200
        assert client.get("/health/live").status_code == 200
        assert client.get("/internal/parts/missing").status_code == 401
        assert client.get("/internal/parts/missing", headers=headers).status_code == 404
        assert client.get("/internal/services/missing", headers=headers).status_code == 404

        payload = {"name": "Oil", "unit_price": "10.00", "available_stock": 3}
        main.app.dependency_overrides[get_actor] = lambda: Actor(is_admin=False, customer_id="customer-1")
        assert client.put("/parts/oil", json=payload, headers=headers).status_code == 403
        main.app.dependency_overrides[get_actor] = lambda: Actor(is_admin=True, customer_id=None)
        created = client.put("/parts/oil", json=payload, headers=headers)
        assert created.status_code == 200
        updated = client.put(
            "/parts/oil",
            json={**payload, "name": "Synthetic oil", "available_stock": 5},
            headers=headers,
        )
        assert updated.json()["name"] == "Synthetic oil"
        assert client.get("/internal/parts/oil", headers=headers).json()["available_stock"] == 5
        assert client.get("/parts/oil").status_code == 200

        service_payload = {"name": "Inspection", "unit_price": "20.00"}
        assert client.put("/services/inspection", json=service_payload, headers=headers).status_code == 200
        assert client.get("/internal/services/inspection", headers=headers).json()["name"] == "Inspection"

        main.app.dependency_overrides[get_actor] = lambda: Actor(is_admin=False, customer_id="customer-1")
        assert client.put("/parts/oil", json=payload, headers=headers).status_code == 403
        assert client.put("/services/inspection", json=service_payload, headers=headers).status_code == 403
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()


def test_reservation_validation_and_idempotent_release(monkeypatch):
    client, engine = client_for_test(monkeypatch)
    headers = {"X-Internal-Token": "test-internal"}
    try:
        unknown = client.post(
            "/reservations",
            json={"order_id": "unknown", "items": [{"sku": "missing", "quantity": 1}]},
            headers=headers,
        )
        assert unknown.status_code == 409

        client.put(
            "/parts/inactive",
            json={"name": "Inactive", "unit_price": "5.00", "available_stock": 4, "active": False},
            headers=headers,
        )
        inactive = client.post(
            "/reservations",
            json={"order_id": "inactive", "items": [{"sku": "inactive", "quantity": 1}]},
            headers=headers,
        )
        assert inactive.status_code == 409

        client.put(
            "/parts/oil",
            json={"name": "Oil", "unit_price": "10.00", "available_stock": 4},
            headers=headers,
        )
        payload = {"order_id": "order-duplicate", "items": [{"sku": "oil", "quantity": 1}]}
        assert client.post("/reservations", json=payload, headers=headers).status_code == 200
        changed_payload = {"order_id": "order-duplicate", "items": [{"sku": "oil", "quantity": 2}]}
        assert client.post("/reservations", json=changed_payload, headers=headers).status_code == 409
        assert client.delete("/reservations/order-duplicate", headers=headers).json()["status"] == "RELEASED"
        assert client.delete("/reservations/order-duplicate", headers=headers).json()["status"] == "ALREADY_RELEASED"
        assert client.delete("/reservations/unknown", headers=headers).json()["status"] == "ALREADY_RELEASED"

        with main.SessionLocal() as db:
            db.add(main.Reservation(order_id="broken", items=[{"sku": "removed", "quantity": 1}]))
            db.commit()
        broken = client.delete("/reservations/broken", headers=headers)
        assert broken.status_code == 409
        assert broken.json()["detail"] == "reserved part is missing: removed"
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()


def test_unknown_inventory_command_is_rejected(monkeypatch):
    client, engine = client_for_test(monkeypatch)
    try:
        with pytest.raises(ValueError, match="unsupported inventory command"):
            main.handle_inventory_command("Unknown", {})
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()
