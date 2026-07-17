from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests
import yaml

from faults import docker_util

SCENARIOS_PATH = Path(__file__).with_name("scenarios.yaml")
SYSTEM_BASE_URL = os.environ.get("SYSTEM_BASE_URL", "http://localhost:8000")
SERVICES = ["app", "postgres", "redis"]


def load_scenarios() -> dict[str, dict]:
    with open(SCENARIOS_PATH) as fh:
        data = yaml.safe_load(fh)
    return {s["id"]: s for s in data}


def _admin(path: str, payload: dict | None = None) -> dict:
    resp = requests.post(f"{SYSTEM_BASE_URL}{path}", json=payload or {}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _dispatch(action: dict) -> dict:
    kind = action["action"]
    if kind == "stop_container":
        return docker_util.stop_container(action["service"])
    if kind == "start_container":
        return docker_util.start_container(action["service"])
    if kind == "admin_inject":
        return _admin("/admin/inject", action.get("payload", {}))
    if kind == "admin_clear":
        return _admin("/admin/clear")
    if kind == "reset":
        return _admin("/admin/reset")
    if kind == "noop":
        return {"noop": True}
    raise ValueError(f"unknown action: {kind}")


def generate_load(n: int = 30) -> int:
    """Fire n cheap requests so request-driven signals (error_rate, load) show up.
    Reused by benign scenarios' `traffic` hint and the Day-9 eval harness."""
    ok = 0
    for _ in range(n):
        try:
            requests.get(f"{SYSTEM_BASE_URL}/work", timeout=5)
            ok += 1
        except requests.RequestException:
            pass
    return ok


def cmd_list(scenarios: dict[str, dict]) -> None:
    for sid, s in scenarios.items():
        tag = "benign" if s.get("benign") else s["type"]
        print(f"  {sid:20s} [{tag}]  {s['name']}")
        print(f"  {'':20s}   root cause: {s['root_cause'].strip()}")


def cmd_status() -> None:
    print("containers:")
    for svc in SERVICES:
        st = docker_util.container_status(svc)
        flag = "UP  " if st["running"] else "DOWN"
        extra = f" exit={st['exit_code']}" if st.get("exit_code") else ""
        print(f"  {flag} {svc:10s} status={st['status']}{extra}")
    try:
        state = requests.get(f"{SYSTEM_BASE_URL}/admin/state", timeout=5).json()
        print(f"injected app faults: {state}")
    except Exception as exc:  # noqa: BLE001 — app may be down on purpose
        print(f"injected app faults: <app unreachable: {exc}>")


def cmd_inject(scenarios: dict[str, dict], sid: str) -> None:
    s = scenarios[sid]
    print(f"injecting '{sid}': {s['name']}")
    result = _dispatch(s["inject"])
    print(f"  -> {result}")


def cmd_clear(scenarios: dict[str, dict], sid: str) -> None:
    s = scenarios[sid]
    print(f"clearing '{sid}'")
    result = _dispatch(s["clear"])
    print(f"  -> {result}")


def cmd_clear_all(scenarios: dict[str, dict]) -> None:
    """Return the system to a known-good state regardless of what's active."""
    for svc in SERVICES:
        st = docker_util.container_status(svc)
        if st["found"] and not st["running"]:
            print(f"  starting {svc}")
            docker_util.start_container(svc)
    try:
        _admin("/admin/clear")
        print("  cleared injected app faults")
    except Exception as exc:  # noqa: BLE001
        print(f"  (could not clear app faults: {exc})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="faults.injector")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("status")
    sub.add_parser("clear-all")
    lp = sub.add_parser("load")
    lp.add_argument("n", nargs="?", type=int, default=30)
    for name in ("inject", "clear"):
        p = sub.add_parser(name)
        p.add_argument("id")
    args = parser.parse_args(argv)

    scenarios = load_scenarios()
    if args.cmd in ("inject", "clear") and args.id not in scenarios:
        print(f"unknown scenario '{args.id}'. known: {', '.join(scenarios)}", file=sys.stderr)
        return 2

    if args.cmd == "list":
        cmd_list(scenarios)
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "clear-all":
        cmd_clear_all(scenarios)
    elif args.cmd == "load":
        print(f"generated {generate_load(args.n)}/{args.n} requests")
    elif args.cmd == "inject":
        cmd_inject(scenarios, args.id)
    elif args.cmd == "clear":
        cmd_clear(scenarios, args.id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())