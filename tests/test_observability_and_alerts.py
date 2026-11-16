from fastapi.testclient import TestClient

from app import main, observability
from app.auth import Actor, get_actor
from tests.test_api import client_for_test

HEADERS = {"X-Internal-Token": "test-internal"}


def seed(client, sku, stock, active=True):
    response = client.put(
        f"/parts/{sku}",
        json={"name": sku.title(), "unit_price": "20.00", "available_stock": stock, "active": active},
    )
    assert response.status_code == 200


def test_low_stock_listing_and_alert_after_reservation(monkeypatch, caplog):
    client, engine = client_for_test(monkeypatch)
    monkeypatch.setattr(main, "LOW_STOCK_THRESHOLD", 2)
    try:
        seed(client, "filter", 3)
        seed(client, "oil", 10)
        seed(client, "old-part", 0, active=False)
        assert client.get("/parts?low_stock=true").json() == []
        assert [p["sku"] for p in client.get("/parts").json()] == ["filter", "oil", "old-part"]

        with caplog.at_level("WARNING", logger="inventory-service"):
            reserved = client.post(
                "/reservations", json={"order_id": "os-1", "items": [{"sku": "filter", "quantity": 2}]}, headers=HEADERS
            )
        assert reserved.status_code == 200
        assert any("low_stock sku=filter" in record.getMessage() for record in caplog.records)
        low = client.get("/parts?low_stock=true").json()
        assert [(p["sku"], p["available_stock"]) for p in low] == [("filter", 1)]

        metrics = client.get("/metrics").json()
        assert metrics["service"] == "inventory-service"
        assert metrics["stock_reservations"]["reserved"] >= 1
        assert metrics["low_stock_alerts"]["filter"] >= 1
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()


def test_reservation_lookup_and_failure_metrics(monkeypatch):
    client, engine = client_for_test(monkeypatch)
    try:
        seed(client, "pad", 1)
        before = observability.METRICS.get("stock_reservations", {}).get("failed", 0)
        failed = client.post(
            "/reservations", json={"order_id": "os-2", "items": [{"sku": "pad", "quantity": 5}]}, headers=HEADERS
        )
        assert failed.status_code == 409
        assert observability.METRICS["stock_reservations"]["failed"] == before + 1
        assert client.get("/reservations/os-2").status_code == 404

        client.post("/reservations", json={"order_id": "os-3", "items": [{"sku": "pad", "quantity": 1}]}, headers=HEADERS)
        assert client.get("/reservations/os-3").json() == {
            "order_id": "os-3",
            "items": [{"sku": "pad", "quantity": 1}],
            "status": "RESERVED",
        }
        assert client.delete("/reservations/os-3", headers=HEADERS).json()["status"] == "RELEASED"
        assert observability.METRICS["stock_reservations"]["released"] >= 1
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()


def test_listing_requires_admin(monkeypatch):
    client, engine = client_for_test(monkeypatch)
    main.app.dependency_overrides[get_actor] = lambda: Actor(is_admin=False, customer_id="c-1")
    try:
        assert client.get("/parts?low_stock=true").status_code == 403
        assert client.get("/reservations/os-1").status_code == 403
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()


def test_correlation_id_is_echoed(monkeypatch):
    client, engine = client_for_test(monkeypatch)
    try:
        response = TestClient(main.app).get("/health/live", headers={"X-Correlation-ID": "corr-demo"})
        assert response.headers["X-Correlation-ID"] == "corr-demo"
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()
