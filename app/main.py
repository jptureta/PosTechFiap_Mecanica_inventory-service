import os
import logging
from decimal import Decimal
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, CheckConstraint, JSON, Integer, Numeric, String, create_engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.auth import Actor, get_actor

logger = logging.getLogger(__name__)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./inventory-service.db")
INTERNAL_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")
engine_options = {"connect_args": {"check_same_thread": False}} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, **engine_options)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Part(Base):
    __tablename__ = "parts"
    __table_args__ = (CheckConstraint("available_stock >= 0", name="ck_parts_stock_nonnegative"),)

    sku: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    available_stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ServiceCatalogItem(Base):
    __tablename__ = "service_catalog"

    sku: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Reservation(Base):
    __tablename__ = "reservations"

    order_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    items: Mapped[list] = mapped_column(JSON, nullable=False)


class PartUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    unit_price: Decimal = Field(gt=0)
    available_stock: int = Field(ge=0)
    active: bool = True


class ServiceUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    unit_price: Decimal = Field(gt=0)
    active: bool = True


class CatalogItemResponse(BaseModel):
    sku: str
    name: str
    unit_price: Decimal
    active: bool
    available_stock: int | None = None
    model_config = {"from_attributes": True}


class RequestedItem(BaseModel):
    sku: str = Field(min_length=1, max_length=100)
    quantity: int = Field(gt=0)


class ReserveItems(BaseModel):
    order_id: str
    items: list[RequestedItem]


def get_db():
    with SessionLocal() as session:
        yield session


def require_internal_token(x_internal_token: Annotated[str | None, Header()] = None) -> None:
    if not INTERNAL_TOKEN or x_internal_token != INTERNAL_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid internal service token")


def require_admin_actor(actor: Actor = Depends(get_actor)) -> Actor:
    if not actor.is_admin:
        raise HTTPException(status_code=403, detail="only an administrator can update the catalog")
    return actor


app = FastAPI(title="Estoque e Catálogo", version="1.0.0")


@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.exception("database readiness check failed")
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok", "service": "inventory-service"}


@app.get("/health/live")
def liveness():
    return {"status": "ok", "service": "inventory-service"}


@app.get("/parts/{sku}", response_model=CatalogItemResponse)
def get_part(sku: str, _: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    part = db.get(Part, sku)
    if part is None:
        raise HTTPException(status_code=404, detail="part not found")
    return part


@app.get("/internal/parts/{sku}", response_model=CatalogItemResponse)
def get_part_for_service(
    sku: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_internal_token),
):
    part = db.get(Part, sku)
    if part is None:
        raise HTTPException(status_code=404, detail="part not found")
    return part


@app.get("/internal/services/{sku}", response_model=CatalogItemResponse)
def get_service_for_service(
    sku: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_internal_token),
):
    service = db.get(ServiceCatalogItem, sku)
    if service is None:
        raise HTTPException(status_code=404, detail="service not found")
    return service


@app.put("/parts/{sku}")
def upsert_part(
    sku: str,
    payload: PartUpdate,
    db: Session = Depends(get_db),
    _: Actor = Depends(require_admin_actor),
):
    part = db.get(Part, sku)
    if part is None:
        part = Part(
            sku=sku,
            name=payload.name,
            unit_price=payload.unit_price,
            available_stock=payload.available_stock,
            active=payload.active,
        )
        db.add(part)
    else:
        part.name = payload.name
        part.unit_price = payload.unit_price
        part.available_stock = payload.available_stock
        part.active = payload.active
    db.commit()
    db.refresh(part)
    return part


@app.put("/services/{sku}")
def upsert_service(
    sku: str,
    payload: ServiceUpdate,
    db: Session = Depends(get_db),
    _: Actor = Depends(require_admin_actor),
):
    service = db.get(ServiceCatalogItem, sku)
    if service is None:
        service = ServiceCatalogItem(
            sku=sku,
            name=payload.name,
            unit_price=payload.unit_price,
            active=payload.active,
        )
        db.add(service)
    else:
        service.name = payload.name
        service.unit_price = payload.unit_price
        service.active = payload.active
    db.commit()
    db.refresh(service)
    return service


@app.post("/reservations")
def reserve(
    payload: ReserveItems,
    db: Session = Depends(get_db),
    _: None = Depends(require_internal_token),
):
    quantities: dict[str, int] = {}
    for item in payload.items:
        quantities[item.sku] = quantities.get(item.sku, 0) + item.quantity
    requested = [{"sku": sku, "quantity": quantities[sku]} for sku in sorted(quantities)]
    existing = db.get(Reservation, payload.order_id)
    if existing is not None:
        if existing.items != requested:
            raise HTTPException(status_code=409, detail="order already has a different reservation")
        return {"order_id": existing.order_id, "items": existing.items, "status": "RESERVED"}

    try:
        parts = {}
        for item in requested:
            part = db.scalar(select(Part).where(Part.sku == item["sku"]).with_for_update())
            if part is None:
                raise HTTPException(status_code=409, detail=f"unknown part: {item['sku']}")
            if not part.active:
                raise HTTPException(status_code=409, detail=f"inactive part: {item['sku']}")
            parts[item["sku"]] = part
        existing = db.scalar(
            select(Reservation)
            .where(Reservation.order_id == payload.order_id)
            .with_for_update()
        )
        if existing is not None:
            if existing.items != requested:
                raise HTTPException(status_code=409, detail="order already has a different reservation")
            result = {"order_id": existing.order_id, "items": existing.items, "status": "RESERVED"}
            db.rollback()
            return result
        for item in requested:
            if parts[item["sku"]].available_stock < item["quantity"]:
                raise HTTPException(status_code=409, detail=f"insufficient stock: {item['sku']}")
            parts[item["sku"]].available_stock -= item["quantity"]
        db.add(Reservation(order_id=payload.order_id, items=requested))
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    return {"order_id": payload.order_id, "items": requested, "status": "RESERVED"}


@app.delete("/reservations/{order_id}")
def release(
    order_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_internal_token),
):
    reservation = db.scalar(
        select(Reservation)
        .where(Reservation.order_id == order_id)
        .with_for_update()
    )
    if reservation is None:
        return {"order_id": order_id, "status": "ALREADY_RELEASED"}
    try:
        for item in reservation.items:
            part = db.scalar(select(Part).where(Part.sku == item["sku"]).with_for_update())
            if part is None:
                raise HTTPException(status_code=409, detail=f"reserved part is missing: {item['sku']}")
            part.available_stock += item["quantity"]
        db.delete(reservation)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    return {"order_id": order_id, "status": "RELEASED"}
