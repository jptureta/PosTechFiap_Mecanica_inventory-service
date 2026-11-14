import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

from app import auth


def credential(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_get_actor_requires_configured_secret(monkeypatch):
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)

    with pytest.raises(HTTPException) as error:
        auth.get_actor(credential("token"))

    assert error.value.status_code == 503


def test_get_actor_accepts_admin_and_customer_tokens(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    admin = jwt.encode({"sub": "admin-1", "tipo": "admin"}, "test-secret", algorithm="HS256")
    customer = jwt.encode(
        {"sub": "customer-user", "tipo": "cliente", "cliente_id": 42},
        "test-secret",
        algorithm="HS256",
    )

    assert auth.get_actor(credential(admin)) == auth.Actor(is_admin=True, customer_id=None)
    assert auth.get_actor(credential(customer)) == auth.Actor(is_admin=False, customer_id="42")


def test_get_actor_rejects_invalid_and_incomplete_tokens(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    unsupported = jwt.encode({"sub": "user-1", "tipo": "other"}, "test-secret", algorithm="HS256")
    incomplete_customer = jwt.encode({"sub": "user-2", "tipo": "cliente"}, "test-secret", algorithm="HS256")

    with pytest.raises(HTTPException) as invalid_error:
        auth.get_actor(credential("not-a-token"))
    assert invalid_error.value.status_code == 401

    with pytest.raises(HTTPException) as incomplete_error:
        auth.get_actor(credential(incomplete_customer))
    assert incomplete_error.value.status_code == 401

    with pytest.raises(HTTPException) as unsupported_error:
        auth.get_actor(credential(unsupported))
    assert unsupported_error.value.status_code == 401
