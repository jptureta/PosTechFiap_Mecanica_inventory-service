from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import Actor, get_actor
from app import main


def client_for_test():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    main.Base.metadata.create_all(engine)
    main.engine = engine
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
