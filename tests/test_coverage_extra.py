import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

from app import auth, main
from app.auth import Actor, get_actor
from tests.test_api import client_for_test


def cred(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_auth_flows(monkeypatch):
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    with pytest.raises(HTTPException) as exc:
        auth.get_actor(cred("x"))
    assert exc.value.status_code == 503

    monkeypatch.setenv("JWT_SECRET_KEY", "s1")
    with pytest.raises(HTTPException) as exc:
        auth.get_actor(cred("invalid"))
    assert exc.value.status_code == 401

    admin_tok = jwt.encode({"sub": "a", "tipo": "admin"}, "s1", algorithm="HS256")
    assert auth.get_actor(cred(admin_tok)).is_admin is True

    cust_tok = jwt.encode({"sub": "c", "tipo": "cliente", "cliente_id": "c1"}, "s1", algorithm="HS256")
    assert auth.get_actor(cred(cust_tok)) == Actor(is_admin=False, customer_id="c1")

    with pytest.raises(HTTPException):
        auth.get_actor(cred(jwt.encode({"sub": "c", "tipo": "cliente"}, "s1", algorithm="HS256")))
    with pytest.raises(HTTPException):
        auth.get_actor(cred(jwt.encode({"sub": "c", "tipo": "x"}, "s1", algorithm="HS256")))


def test_catalog_endpoints_and_reservation_edge_cases():
    client, engine = client_for_test()
    headers = {"X-Internal-Token": "test-internal"}
    try:
        assert client.get("/health").status_code == 200
        assert client.get("/health/live").status_code == 200
        assert client.get("/metrics").json()["service"] == "inventory-service"

        assert client.get("/parts/missing").status_code == 404
        assert client.get("/internal/parts/missing", headers=headers).status_code == 404
        assert client.get("/internal/services/missing", headers=headers).status_code == 404
        assert client.get("/internal/parts/x").status_code == 401

        client.put("/parts/p1", json={"name": "Part 1", "unit_price": "20.00", "available_stock": 4, "active": True})
        client.put("/parts/p1", json={"name": "Part 1 v2", "unit_price": "22.00", "available_stock": 5, "active": True})
        assert client.get("/parts/p1").json()["name"] == "Part 1 v2"
        assert client.get("/internal/parts/p1", headers=headers).json()["available_stock"] == 5

        client.put("/services/s1", json={"name": "Svc 1", "unit_price": "50.00", "active": True})
        client.put("/services/s1", json={"name": "Svc 1 v2", "unit_price": "55.00", "active": True})
        assert client.get("/internal/services/s1", headers=headers).json()["name"] == "Svc 1 v2"

        # Idempotent reservation and conflicting reservation
        res1 = client.post(
            "/reservations",
            json={"order_id": "ord-1", "items": [{"sku": "p1", "quantity": 2}]},
            headers=headers,
        )
        assert res1.status_code == 200
        res_same = client.post(
            "/reservations",
            json={"order_id": "ord-1", "items": [{"sku": "p1", "quantity": 2}]},
            headers=headers,
        )
        assert res_same.status_code == 200
        res_conflict = client.post(
            "/reservations",
            json={"order_id": "ord-1", "items": [{"sku": "p1", "quantity": 1}]},
            headers=headers,
        )
        assert res_conflict.status_code == 409

        # Unknown and inactive part reservation errors
        assert (
            client.post(
                "/reservations",
                json={"order_id": "ord-2", "items": [{"sku": "unknown", "quantity": 1}]},
                headers=headers,
            ).status_code
            == 409
        )
        client.put("/parts/inactive", json={"name": "Old", "unit_price": "10.00", "available_stock": 5, "active": False})
        assert (
            client.post(
                "/reservations",
                json={"order_id": "ord-3", "items": [{"sku": "inactive", "quantity": 1}]},
                headers=headers,
            ).status_code
            == 409
        )

        # Release and repeat release
        assert client.delete("/reservations/ord-1", headers=headers).json()["status"] == "RELEASED"
        assert client.delete("/reservations/ord-1", headers=headers).json()["status"] == "ALREADY_RELEASED"

        # Non-admin forbidden on catalog update
        main.app.dependency_overrides[get_actor] = lambda: Actor(is_admin=False, customer_id="c1")
        assert (
            client.put("/parts/p1", json={"name": "X", "unit_price": "10.00", "available_stock": 1, "active": True}).status_code
            == 403
        )
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()
