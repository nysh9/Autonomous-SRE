"""Read-only tools the agent uses to investigate the system."""
from __future__ import annotations

import argparse
import json
import os
import time

import requests

from faults import docker_util

SYSTEM_BASE_URL = os.environ.get("SYSTEM_BASE_URL", "http://localhost:8000")
HTTP_TIMEOUT = 5

TOPOLOGY = {
    "app": {"role": "api", "depends_on": ["postgres", "redis"]},
    "postgres": {"role": "database", "depends_on": []},
    "redis": {"role": "cache", "depends_on": []},
}
SERVICES = list(TOPOLOGY)


def _http_get(path):
    url = f"{SYSTEM_BASE_URL}{path}"
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT)
        return {"ok": True, "reachable": True, "http_status": resp.status_code, "body": resp.json()}
    except requests.exceptions.ConnectionError as exc:
        return {"ok": False, "reachable": False, "error": f"connection refused: {exc.__class__.__name__}", "url": url}
    except requests.exceptions.Timeout:
        return {"ok": False, "reachable": False, "error": "request timed out", "url": url}
    except ValueError as exc:
        return {"ok": False, "reachable": True, "error": f"non-JSON response: {exc}", "url": url}


def list_services():
    services = []
    for svc in SERVICES:
        status = docker_util.container_status(svc)
        services.append({
            "service": svc,
            "role": TOPOLOGY[svc]["role"],
            "depends_on": TOPOLOGY[svc]["depends_on"],
            "running": status["running"],
            "status": status["status"],
            "exit_code": status.get("exit_code"),
            "health": status.get("health"),
        })
    return {"ok": True, "services": services}


def get_metrics(metric=None):
    res = _http_get("/metrics")
    if not res["ok"]:
        return {"ok": False, "service": "app", "reachable": res["reachable"], "error": res["error"]}
    metrics = res["body"]
    if metric is not None:
        if metric not in metrics:
            return {"ok": False, "service": "app", "error": f"unknown metric '{metric}'",
                    "available": sorted(metrics.keys())}
        return {"ok": True, "service": "app", "metric": metric, "value": metrics[metric]}
    return {"ok": True, "service": "app", "metrics": metrics}


def get_recent_logs(service, since_seconds=300, tail=100):
    if service not in SERVICES:
        return {"ok": False, "error": f"unknown service '{service}'", "known": SERVICES}
    container = docker_util.find_container(service)
    if container is None:
        return {"ok": False, "service": service, "error": "container not found"}
    since_ts = int(time.time() - since_seconds) if since_seconds else None
    try:
        raw = container.logs(since=since_ts, tail=tail, timestamps=False)
    except Exception as exc:
        return {"ok": False, "service": service, "error": f"could not read logs: {exc}"}

    entries = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append({"json": json.loads(line)})
        except json.JSONDecodeError:
            entries.append({"text": line})
    return {"ok": True, "service": service, "since_seconds": since_seconds,
            "count": len(entries), "logs": entries}


def check_health(service=None):
    targets = [service] if service else SERVICES
    for t in targets:
        if t not in SERVICES:
            return {"ok": False, "error": f"unknown service '{t}'", "known": SERVICES}

    health = _http_get("/health")
    app_checks = health["body"]["checks"] if health["ok"] else {}

    results = []
    for t in targets:
        entry = {"service": t, "container": docker_util.container_status(t)}
        if t == "app":
            if health["ok"]:
                entry["http_health"] = {"status": health["body"].get("status"),
                                        "http_status": health["http_status"],
                                        "checks": app_checks}
            else:
                entry["http_health"] = {"reachable": False, "error": health["error"]}
        else:
            entry["app_sees_up"] = app_checks.get(t) if health["ok"] else None
        results.append(entry)

    if service:
        return {"ok": True, **results[0]}
    return {"ok": True, "services": results}


CONFIG_PREFIXES = ("DATABASE_URL", "REDIS_URL", "DB_POOL", "CACHE_TTL", "POSTGRES_", "SYSTEM_")


def read_config(service):
    if service not in SERVICES:
        return {"ok": False, "error": f"unknown service '{service}'", "known": SERVICES}
    container = docker_util.find_container(service)
    if container is None:
        return {"ok": False, "service": service, "error": "container not found"}
    cfg = container.attrs.get("Config", {})
    env = {}
    for pair in cfg.get("Env", []) or []:
        key, _, value = pair.partition("=")
        if key.startswith(CONFIG_PREFIXES):
            env[key] = value
    return {"ok": True, "service": service, "image": cfg.get("Image"),
            "command": cfg.get("Cmd"), "env": env}


TOOL_SCHEMAS = [
    {
        "name": "list_services",
        "description": (
            "List every service in the system with its role, dependencies, and live "
            "running/stopped status. Use first to understand the topology and spot a down service."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_metrics",
        "description": (
            "Get current app metrics: error_rate, error_count, request_count, p50/p99 latency, "
            "memory_rss_mb, uptime, and dependency up/down. Optionally pass a single metric name. "
            "Returns an error if the app is unreachable (which itself is a signal it is down)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"metric": {"type": "string", "description": "Optional single metric key to fetch."}},
            "required": [],
        },
    },
    {
        "name": "get_recent_logs",
        "description": (
            "Read recent logs for a service's container (app, postgres, or redis). Returns parsed "
            "JSON log lines when available. Works on crashed containers too, so you can see what "
            "happened right before an exit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "enum": SERVICES},
                "since_seconds": {"type": "integer", "description": "Look back this many seconds (default 300)."},
                "tail": {"type": "integer", "description": "Max lines to return (default 100)."},
            },
            "required": ["service"],
        },
    },
    {
        "name": "check_health",
        "description": (
            "Check health of one service or all. For the app, performs a live /health probe with "
            "per-dependency checks plus container state. For postgres/redis, reports container state "
            "and how the app currently sees that dependency."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"service": {"type": "string", "enum": SERVICES,
                                       "description": "Omit to check all services."}},
            "required": [],
        },
    },
    {
        "name": "read_config",
        "description": (
            "Read a service container's effective configuration: image, command, and deploy-relevant "
            "env vars (connection strings, pool sizes, TTLs). Use to spot bad config / wrong env vars."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"service": {"type": "string", "enum": SERVICES}},
            "required": ["service"],
        },
    },
]

TOOL_FUNCTIONS = {
    "list_services": list_services,
    "get_metrics": get_metrics,
    "get_recent_logs": get_recent_logs,
    "check_health": check_health,
    "read_config": read_config,
}


def call_tool(name, **kwargs):
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return {"ok": False, "error": f"unknown tool '{name}'", "known": list(TOOL_FUNCTIONS)}
    try:
        return fn(**kwargs)
    except Exception as exc:
        return {"ok": False, "tool": name, "error": f"{exc.__class__.__name__}: {exc}"}


def _run_one(args):
    if args.tool == "list_services":
        return list_services()
    if args.tool == "get_metrics":
        return get_metrics(metric=args.metric)
    if args.tool == "get_recent_logs":
        return get_recent_logs(service=args.service or "app",
                               since_seconds=args.since_seconds, tail=args.tail)
    if args.tool == "check_health":
        return check_health(service=args.service)
    if args.tool == "read_config":
        return read_config(service=args.service or "app")
    raise ValueError(args.tool)


def _smoke_all():
    print("== list_services =="); print(json.dumps(list_services(), indent=2))
    print("\n== get_metrics =="); print(json.dumps(get_metrics(), indent=2))
    print("\n== check_health (all) =="); print(json.dumps(check_health(), indent=2))
    print("\n== read_config app =="); print(json.dumps(read_config("app"), indent=2))
    print("\n== get_recent_logs app (tail 5) ==")
    print(json.dumps(get_recent_logs("app", tail=5), indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent.tools")
    parser.add_argument("tool", nargs="?", choices=list(TOOL_FUNCTIONS))
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--service")
    parser.add_argument("--metric")
    parser.add_argument("--since-seconds", type=int, default=300, dest="since_seconds")
    parser.add_argument("--tail", type=int, default=100)
    args = parser.parse_args(argv)

    if args.all or args.tool is None:
        _smoke_all()
        return 0
    print(json.dumps(_run_one(args), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())