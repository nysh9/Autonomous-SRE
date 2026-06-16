from __future__ import annotations

import os
import time

import psycopg2
import redis
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from psycopg2 import pool as pgpool
from pydantic import BaseModel

from telemetry import TelemetryMiddleware, log, metrics

DB_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://sre:sre@postgres:5432/sre"
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
DB_POOL_MIN = int(os.environ.get("DB_POOL_MIN", "1"))
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "5"))
CACHE_TTL_S = int(os.environ.get("CACHE_TTL_S", "30"))

app = FastAPI(title="system-under-observation")
app.add_middleware(TelemetryMiddleware)

_db_pool: pgpool.SimpleConnectionPool | None = None
_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=2)
    return _redis


def _ensure_pool() -> pgpool.SimpleConnectionPool:
    global _db_pool
    if _db_pool is None:
        _db_pool = pgpool.SimpleConnectionPool(DB_POOL_MIN, DB_POOL_MAX, dsn=DB_DSN)
    return _db_pool


class _DbConn:
    """Borrow/return a pooled connection; surface pool exhaustion as a 503."""

    def __enter__(self):
        try:
            self.conn = _ensure_pool().getconn()
        except pgpool.PoolError as exc:
            raise HTTPException(status_code=503, detail=f"db pool exhausted: {exc}")
        return self.conn

    def __exit__(self, *exc):
        if getattr(self, "conn", None) is not None:
            _db_pool.putconn(self.conn)


@app.on_event("startup")
def startup() -> None:
    for attempt in range(10):
        try:
            with _DbConn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "CREATE TABLE IF NOT EXISTS items "
                        "(id SERIAL PRIMARY KEY, name TEXT NOT NULL)"
                    )
                conn.commit()
            log.info("startup_db_ready", extra={"attempt": attempt})
            break
        except Exception as exc:  # noqa: BLE001 — startup wants to keep retrying
            log.warning("startup_db_retry", extra={"attempt": attempt, "err": str(exc)})
            time.sleep(1)



class ItemIn(BaseModel):
    name: str


@app.get("/")
def root() -> dict:
    return {"service": "system-under-observation", "ok": True}


@app.get("/health")
def health() -> JSONResponse:
    checks: dict[str, bool] = {}
    try:
        with _DbConn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        checks["postgres"] = True
    except Exception:  # noqa: BLE001
        checks["postgres"] = False
    try:
        checks["redis"] = bool(get_redis().ping())
    except Exception:  # noqa: BLE001
        checks["redis"] = False

    for name, up in checks.items():
        metrics.set_dep(name, up)

    healthy = all(checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "healthy" if healthy else "unhealthy", "checks": checks},
    )


@app.get("/metrics")
def get_metrics() -> dict:
    return metrics.snapshot()


@app.post("/items")
def create_item(item: ItemIn) -> dict:
    with _DbConn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO items (name) VALUES (%s) RETURNING id", (item.name,)
            )
            new_id = cur.fetchone()[0]
        conn.commit()
    get_redis().setex(f"item:{new_id}", CACHE_TTL_S, item.name)
    return {"id": new_id, "name": item.name}


@app.get("/items/{item_id}")
def get_item(item_id: int) -> dict:
    cached = get_redis().get(f"item:{item_id}")
    if cached is not None:
        return {"id": item_id, "name": cached.decode(), "source": "cache"}
    with _DbConn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM items WHERE id = %s", (item_id,))
            row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="item not found")
    name = row[0]
    get_redis().setex(f"item:{item_id}", CACHE_TTL_S, name)
    return {"id": item_id, "name": name, "source": "db"}


@app.get("/work")
def work(fail_rate: float = 0.0, delay_ms: int = 0) -> dict:
    """Synthetic workload + a seam for later error-rate / latency faults."""
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)
    if fail_rate > 0 and (time.time() * 1000) % 100 < fail_rate * 100:
        raise HTTPException(status_code=500, detail="synthetic failure")
    return {"ok": True, "delay_ms": delay_ms, "fail_rate": fail_rate}
