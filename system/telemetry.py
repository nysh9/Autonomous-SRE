"""Telemetry for the system-under-observation.
  1. Structured JSON logs to stdout
  2. In-memory metrics
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections import deque
from threading import Lock

import psutil
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line. Extra fields ride along via `extra={...}`."""

    _RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
        "message",
        "asctime",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(name: str = "system") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonLogFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


log = get_logger()


class Metrics:
    """Thread-safe counters + a rolling window of latencies for percentiles."""

    def __init__(self, window: int = 500) -> None:
        self._lock = Lock()
        self._latencies: deque[float] = deque(maxlen=window)
        self.request_count = 0
        self.error_count = 0
        self.started_at = time.time()
        self.deps: dict[str, bool] = {"postgres": False, "redis": False}

    def record(self, latency_ms: float, is_error: bool) -> None:
        with self._lock:
            self._latencies.append(latency_ms)
            self.request_count += 1
            if is_error:
                self.error_count += 1

    def set_dep(self, name: str, up: bool) -> None:
        with self._lock:
            self.deps[name] = up

    @staticmethod
    def _percentile(values: list[float], pct: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
        return round(ordered[idx], 2)

    def snapshot(self) -> dict:
        with self._lock:
            lat = list(self._latencies)
            req = self.request_count
            err = self.error_count
            deps = dict(self.deps)
            uptime = time.time() - self.started_at
        return {
            "request_count": req,
            "error_count": err,
            "error_rate": round(err / req, 4) if req else 0.0,
            "p50_latency_ms": self._percentile(lat, 50),
            "p99_latency_ms": self._percentile(lat, 99),
            "memory_rss_mb": round(psutil.Process().memory_info().rss / 1_048_576, 1),
            "uptime_s": round(uptime, 1),
            "dependencies": deps,
        }


metrics = Metrics()

class InjectedFaults:
    """In-process fault state toggled by the /admin endpoints (see app.py)
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self.latency_ms = 0
        self.fail_rate = 0.0

    def set(self, latency_ms: int | None = None, fail_rate: float | None = None) -> None:
        with self._lock:
            if latency_ms is not None:
                self.latency_ms = max(0, int(latency_ms))
            if fail_rate is not None:
                self.fail_rate = min(1.0, max(0.0, float(fail_rate)))

    def clear(self) -> None:
        with self._lock:
            self.latency_ms = 0
            self.fail_rate = 0.0

    def snapshot(self) -> dict:
        with self._lock:
            return {"latency_ms": self.latency_ms, "fail_rate": self.fail_rate}


injected = InjectedFaults()


class TelemetryMiddleware(BaseHTTPMiddleware):
    """Time every request, count errors (5xx or raised), emit a JSON access log."""

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            latency_ms = (time.perf_counter() - start) * 1000.0
            is_error = status >= 500
            if request.url.path != "/metrics":
                metrics.record(latency_ms, is_error)
                log.info(
                    "request",
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                        "status": status,
                        "latency_ms": round(latency_ms, 2),
                    },
                )
