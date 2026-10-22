import os
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

bearer = HTTPBearer()


@dataclass(frozen=True)
class Actor:
    is_admin: bool
    customer_id: str | None


def get_actor(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
) -> Actor:
    secret = os.getenv("JWT_SECRET_KEY", "")
    if not secret:
        raise HTTPException(status_code=503, detail="JWT validation is not configured")
    try:
        payload = jwt.decode(
            credentials.credentials,
            secret,
            algorithms=[os.getenv("JWT_ALGORITHM", "HS256")],
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    actor_type = payload.get("tipo", "admin")
    if actor_type == "cliente":
        customer_id = payload.get("cliente_id")
        if customer_id is None or not payload.get("sub"):
            raise HTTPException(status_code=401, detail="invalid customer token")
        return Actor(is_admin=False, customer_id=str(customer_id))
    if actor_type == "admin" and payload.get("sub"):
        return Actor(is_admin=True, customer_id=None)
    raise HTTPException(status_code=401, detail="invalid access token")
