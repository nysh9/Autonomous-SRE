"""Offline regression tests — no Docker, no API key. Run: python -m pytest tests/ -q"""
import agent.tools as tools
from agent.diagnose import MockModel, cost, triage
from faults.injector import load_scenarios

KNOWN_ACTIONS = {"stop_container", "start_container", "admin_inject", "admin_clear"}


def _healthy_snapshot(**metrics):
    m = {"error_rate": 0.0, "p99_latency_ms": 12.0}
    m.update(metrics)
    return {
        "services": [{"service": s, "running": True, "status": "running"}
                     for s in ("app", "postgres", "redis")],
        "metrics": m,
    }


def test_cost_per_tier():
    assert cost({"input": 1_000_000, "output": 0}, "claude-haiku-4-5") == 1.0
    assert cost({"input": 0, "output": 1_000_000}, "claude-sonnet-5") == 15.0
    assert cost({"input": 1_000_000, "output": 1_000_000}, "claude-opus-4-8") == 90.0
    assert cost({"input": 999, "output": 999}, "some-unknown-model") == 0.0


def test_triage_quiet_when_healthy():
    assert triage(MockModel(), _healthy_snapshot())["incident"] is False


def test_triage_escalates_on_down_dependency():
    snap = _healthy_snapshot()
    snap["services"][1]["running"] = False
    snap["services"][1]["status"] = "exited"
    assert triage(MockModel(), snap)["incident"] is True


def test_triage_escalates_on_high_error_rate():
    assert triage(MockModel(), _healthy_snapshot(error_rate=0.4))["incident"] is True


def test_triage_escalates_on_high_latency():
    assert triage(MockModel(), _healthy_snapshot(p99_latency_ms=850))["incident"] is True


def test_get_metrics_structured_error_when_app_down(monkeypatch):
    monkeypatch.setattr(tools, "SYSTEM_BASE_URL", "http://localhost:9")
    out = tools.get_metrics()
    assert out["ok"] is False
    assert out["reachable"] is False


def test_extract_snapshot_ignores_trailing_text():
    msgs = [{"role": "user", "content": 'Snapshot:\n{"services": [], "metrics": {}}\n\nDecide now.'}]
    assert MockModel._extract_snapshot(msgs) == {"services": [], "metrics": {}}


def test_scenarios_have_known_actions_and_ground_truth():
    scenarios = load_scenarios()
    assert scenarios
    for sid, s in scenarios.items():
        assert s["inject"]["action"] in KNOWN_ACTIONS, sid
        assert s["clear"]["action"] in KNOWN_ACTIONS, sid
        for field in ("symptom", "root_cause", "correct_fix"):
            assert s.get(field), f"{sid} missing {field}"
