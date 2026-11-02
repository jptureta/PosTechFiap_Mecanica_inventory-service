"""Observabilidade distribuída: Correlation ID, logs JSON estruturados e métricas."""

import contextvars
import json
import logging
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request

correlation_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="")

START_TIME = time.time()
METRICS: dict[str, Any] = {
    "requests_total": 0,
    "requests_by_route": defaultdict(int),
    "requests_by_status": defaultdict(int),
    "events_published": defaultdict(int),
    "events_consumed": defaultdict(int),
}


def get_correlation_id() -> str:
    cid = correlation_id_ctx.get()
    if not cid:
        cid = f"corr-{uuid.uuid4().hex[:12]}"
        correlation_id_ctx.set(cid)
    return cid


def set_correlation_id(cid: str | None) -> str:
    value = cid or f"corr-{uuid.uuid4().hex[:12]}"
    correlation_id_ctx.set(value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "logger": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
            "correlation_id": correlation_id_ctx.get() or None,
        }
        for key in ("service", "method", "route", "status_code", "latency_ms", "order_id", "event_type"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging(service_name: str) -> logging.Logger:
    log = logging.getLogger(service_name)
    log.setLevel(logging.INFO)
    if not any(isinstance(h.formatter, JsonFormatter) for h in log.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        log.addHandler(handler)
    return log


def instrument_app(app: FastAPI, service_name: str) -> None:
    log = configure_logging(service_name)

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        cid = set_correlation_id(request.headers.get("X-Correlation-ID") or request.headers.get("X-Request-ID"))
        start = time.perf_counter()
        response = await call_next(request)
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        route = request.url.path
        METRICS["requests_total"] += 1
        METRICS["requests_by_route"][route] += 1
        METRICS["requests_by_status"][str(response.status_code)] += 1
        response.headers["X-Correlation-ID"] = cid
        log.info(
            "http_request",
            extra={
                "service": service_name,
                "method": request.method,
                "route": route,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
            },
        )
        return response

    @app.get("/metrics")
    def metrics():
        return {
            "service": service_name,
            "uptime_seconds": round(time.time() - START_TIME, 2),
            "requests_total": METRICS["requests_total"],
            "requests_by_route": dict(METRICS["requests_by_route"]),
            "requests_by_status": dict(METRICS["requests_by_status"]),
            "events_published": dict(METRICS["events_published"]),
            "events_consumed": dict(METRICS["events_consumed"]),
        }
