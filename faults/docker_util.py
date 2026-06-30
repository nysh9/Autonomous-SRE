from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

import docker

PROJECT = os.environ.get("COMPOSE_PROJECT", "autonomoussre")


def _resolve_docker_host() -> str | None:
    """Best-effort Docker endpoint resolution for Colima."""
    if os.environ.get("DOCKER_HOST"):
        return os.environ["DOCKER_HOST"]
    # Ask the active docker context (covers non-default Colima profiles).
    try:
        out = subprocess.run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            capture_output=True, text=True, timeout=5,
        )
        host = out.stdout.strip()
        if host:
            return host
    except Exception:  # noqa: BLE001 — fall through to the well-known path
        pass
    sock = Path.home() / ".colima" / "default" / "docker.sock"
    return f"unix://{sock}" if sock.exists() else None


@lru_cache(maxsize=1)
def get_client() -> docker.DockerClient:
    host = _resolve_docker_host()
    if host:
        return docker.DockerClient(base_url=host)
    return docker.from_env()


def find_container(service: str):
    """Return the compose container for a service, or None if absent."""
    client = get_client()
    matches = client.containers.list(
        all=True,
        filters={
            "label": [
                f"com.docker.compose.project={PROJECT}",
                f"com.docker.compose.service={service}",
            ]
        },
    )
    return matches[0] if matches else None


def container_status(service: str) -> dict:
    """Structured status for one service (used by injector + later agent tools)."""
    c = find_container(service)
    if c is None:
        return {"service": service, "found": False, "status": "absent", "running": False}
    state = c.attrs.get("State", {})
    return {
        "service": service,
        "found": True,
        "name": c.name,
        "status": c.status,                       # running | exited | ...
        "running": c.status == "running",
        "exit_code": state.get("ExitCode"),
        "health": state.get("Health", {}).get("Status"),
    }


def stop_container(service: str) -> dict:
    c = find_container(service)
    if c is None:
        raise RuntimeError(f"no container for service '{service}' in project '{PROJECT}'")
    c.stop(timeout=3)
    return container_status(service)


def start_container(service: str) -> dict:
    c = find_container(service)
    if c is None:
        raise RuntimeError(f"no container for service '{service}' in project '{PROJECT}'")
    c.start()
    return container_status(service)
