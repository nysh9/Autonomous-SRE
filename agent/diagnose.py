"""Single-agent diagnosis loop: investigate the system with tools, conclude with a diagnosis.

Two halves: a model wrapper (real Anthropic, or an offline heuristic mock when there's no
key) and the tool-calling loop that drives an investigation to a structured incident report.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from dotenv import load_dotenv

from agent.tools import TOOL_SCHEMAS, call_tool

load_dotenv()

DIAGNOSE_MODEL = os.environ.get("DIAGNOSE_MODEL", "claude-sonnet-5")
TRIAGE_MODEL = os.environ.get("TRIAGE_MODEL", "claude-haiku-4-5-20251001")
MAX_STEPS = 8

# Approximate USD per 1M tokens (input, output). Configurable; used to turn token
# counts into a dollar cost so the triage/diagnose split can be measured.
PRICING = {"haiku": (1.0, 5.0), "sonnet": (3.0, 15.0), "opus": (15.0, 75.0)}


def cost(tokens: dict, model: str) -> float:
    key = next((k for k in PRICING if k in model), None)
    if key is None:
        return 0.0
    pin, pout = PRICING[key]
    return round(tokens["input"] / 1e6 * pin + tokens["output"] / 1e6 * pout, 6)

SYSTEM_PROMPT = """You are an SRE diagnostician. A monitoring alert may have fired on a small \
service and you must find out what (if anything) is wrong.

System topology:
  - app (FastAPI api) depends on -> postgres (database), redis (cache)
  - postgres: the database
  - redis: the cache

You have read-only tools to inspect services, metrics, logs, health, and config. Investigate \
methodically: get the lay of the land first, then drill into whatever looks off. When you are \
confident, finish by calling submit_diagnosis exactly once. Do not call it until you have \
gathered enough evidence. If everything is healthy, say so (incident: false)."""

SUBMIT_DIAGNOSIS = {
    "name": "submit_diagnosis",
    "description": "Record your final conclusion. Call exactly once, after investigating.",
    "input_schema": {
        "type": "object",
        "properties": {
            "incident": {"type": "boolean", "description": "True if something is actually wrong."},
            "root_cause": {"type": "string", "description": "The underlying cause, specific."},
            "recommended_fix": {"type": "string", "description": "What an operator should do."},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "evidence": {"type": "array", "items": {"type": "string"},
                         "description": "Concrete signals that support the conclusion."},
        },
        "required": ["incident", "root_cause", "recommended_fix", "confidence", "evidence"],
    },
}

TRIAGE_SYSTEM = """You are the first-line triage for an SRE agent. You get a compact snapshot of a \
small service (app -> postgres, redis) and must make ONE fast call: is something actually wrong? \
Do not diagnose the root cause — a stronger agent does that. Only decide whether to escalate. \
Any service not running, unreachable metrics, elevated error rate, or elevated latency means \
incident. If unsure, say incident with low confidence. Call triage_verdict."""

TRIAGE_VERDICT = {
    "name": "triage_verdict",
    "description": "Your triage decision. Call exactly once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "incident": {"type": "boolean", "description": "True if the strong agent should investigate."},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "reason": {"type": "string", "description": "One line: what tipped the decision."},
        },
        "required": ["incident", "confidence", "reason"],
    },
}


@dataclass
class LLMResponse:
    content: list
    tool_calls: list = field(default_factory=list)
    text: str = ""
    stop_reason: str = "end_turn"
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})


class AnthropicModel:
    def __init__(self):
        import anthropic
        self._anthropic = anthropic
        # SDK retries transient errors (429/5xx/overloaded/connection) with backoff.
        self.client = anthropic.Anthropic(timeout=30.0, max_retries=2)

    def complete(self, system, messages, tools, model, tool_choice=None) -> LLMResponse:
        kwargs = dict(model=model, system=system, tools=tools, messages=messages, max_tokens=1500)
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        try:
            resp = self.client.messages.create(**kwargs)
        except self._anthropic.APIError as exc:
            # Bad model id, exhausted retries, etc. — surface as a clean sentinel, don't crash.
            return LLMResponse(content=[], tool_calls=[], text=f"[api_error] {exc}", stop_reason="error")
        tool_calls, text = [], ""
        for block in resp.content:
            if block.type == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "input": block.input})
            elif block.type == "text":
                text += block.text
        return LLMResponse(
            content=resp.content, tool_calls=tool_calls, text=text,
            stop_reason=resp.stop_reason,
            usage={"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens},
        )


class MockModel:
    """Offline stand-in: emulates tool-calling, then diagnoses with simple heuristics.

    Honestly rule-based, not reasoning — it exists so the loop runs without a key.
    """
    _n = 0

    def complete(self, system, messages, tools, model, tool_choice=None) -> LLMResponse:
        if any(t["name"] == "triage_verdict" for t in tools):
            verdict = self._triage(self._extract_snapshot(messages))
            block = {"type": "tool_use", "id": "mock_triage", "name": "triage_verdict", "input": verdict}
            return LLMResponse(content=[block],
                               tool_calls=[{"id": "mock_triage", "name": "triage_verdict", "input": verdict}],
                               stop_reason="tool_use")
        seen_results = any(
            isinstance(m["content"], list)
            and any(b.get("type") == "tool_result" for b in m["content"])
            for m in messages if isinstance(m.get("content"), list)
        )
        if not seen_results:
            calls = [{"id": f"mock_{t}", "name": t, "input": {}}
                     for t in ("list_services", "get_metrics", "check_health")]
            blocks = [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": {}} for c in calls]
            return LLMResponse(content=blocks, tool_calls=calls, stop_reason="tool_use")

        diag = self._diagnose(messages)
        block = {"type": "tool_use", "id": "mock_submit", "name": "submit_diagnosis", "input": diag}
        return LLMResponse(content=[block],
                           tool_calls=[{"id": "mock_submit", "name": "submit_diagnosis", "input": diag}],
                           stop_reason="tool_use")

    @staticmethod
    def _collect(messages) -> dict:
        out = {}
        for m in messages:
            if not isinstance(m.get("content"), list):
                continue
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    try:
                        out[b["tool_use_id"]] = json.loads(b["content"])
                    except (ValueError, TypeError):
                        pass
        return out

    def _diagnose(self, messages) -> dict:
        results = self._collect(messages)
        services = results.get("mock_list_services", {}).get("services", [])
        by_name = {s["service"]: s for s in services}
        metrics = results.get("mock_get_metrics", {}).get("metrics", {})

        if by_name.get("app") and not by_name["app"]["running"]:
            return {"incident": True, "root_cause": "The app container is not running (crashed/stopped).",
                    "recommended_fix": "Restart the app container.", "confidence": "high",
                    "evidence": ["app container status is not 'running'"]}
        for dep in ("postgres", "redis"):
            if by_name.get(dep) and not by_name[dep]["running"]:
                return {"incident": True, "root_cause": f"The {dep} container is down.",
                        "recommended_fix": f"Restart the {dep} container.", "confidence": "high",
                        "evidence": [f"{dep} container status is not 'running'"]}
        p99 = metrics.get("p99_latency_ms", 0)
        if p99 and p99 > 300 and metrics.get("error_rate", 0) < 0.1:
            return {"incident": True, "root_cause": "Elevated request latency (slow path / injected delay).",
                    "recommended_fix": "Remove the injected latency or fix the slow dependency.",
                    "confidence": "medium", "evidence": [f"p99 latency {p99}ms far above baseline"]}
        # All containers are up by this point, so high errors mean the app itself is failing.
        err = metrics.get("error_rate", 0)
        if err and err > 0.2:
            return {"incident": True, "root_cause": "The app is failing a portion of requests (bad code path / deploy), not a dependency outage.",
                    "recommended_fix": "Roll back or fix the failing endpoint.", "confidence": "medium",
                    "evidence": [f"error_rate {err} well above baseline", "all dependencies healthy"]}
        return {"incident": False, "root_cause": "No fault detected; all services healthy.",
                "recommended_fix": "No action needed.", "confidence": "medium",
                "evidence": ["all containers running", "error rate near zero"]}

    @staticmethod
    def _extract_snapshot(messages) -> dict:
        decoder = json.JSONDecoder()
        for m in reversed(messages):
            content = m.get("content")
            if isinstance(content, str) and "{" in content:
                try:
                    obj, _ = decoder.raw_decode(content[content.index("{"):])
                    return obj
                except ValueError:
                    continue
        return {}

    @staticmethod
    def _triage(snapshot) -> dict:
        services = snapshot.get("services", [])
        metrics = snapshot.get("metrics", {})
        down = [s["service"] for s in services if not s.get("running", True)]
        if down:
            return {"incident": True, "confidence": "high", "reason": f"{', '.join(down)} not running"}
        if not metrics or metrics.get("reachable") is False:
            return {"incident": True, "confidence": "high", "reason": "app metrics unreachable"}
        if metrics.get("error_rate", 0) > 0.2:
            return {"incident": True, "confidence": "high", "reason": "elevated error rate"}
        if metrics.get("p99_latency_ms", 0) > 300:
            return {"incident": True, "confidence": "medium", "reason": "elevated p99 latency"}
        return {"incident": False, "confidence": "high", "reason": "all services running, metrics nominal"}


def _make_model():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicModel(), "anthropic"
    return MockModel(), "mock"


def _summarize(out: dict) -> str:
    if not out.get("ok", True):
        return f"error: {out.get('error')}"
    if "services" in out:
        parts = []
        for s in out["services"]:
            if "status" in s:
                parts.append(f"{s['service']}={s['status']}")
            elif "container" in s:
                parts.append(f"{s['service']}={s['container']['status']}")
        return ", ".join(parts)
    if "container" in out:
        return f"{out['service']}={out['container']['status']} app_sees_up={out.get('app_sees_up')}"
    if "metrics" in out:
        m = out["metrics"]
        return f"err_rate={m.get('error_rate')} p99={m.get('p99_latency_ms')}ms deps={m.get('dependencies')}"
    if "logs" in out:
        return f"{out['count']} log lines"
    if "http_health" in out or "container" in out:
        return json.dumps({k: out[k] for k in out if k in ("http_health", "container", "app_sees_up")})
    return json.dumps(out)[:200]


def diagnose(model_name: str = DIAGNOSE_MODEL, max_steps: int = MAX_STEPS) -> dict:
    model, backend = _make_model()
    tools = TOOL_SCHEMAS + [SUBMIT_DIAGNOSIS]
    messages = [{"role": "user",
                 "content": "A monitoring alert fired. Investigate the system and diagnose the incident."}]
    trace: list = []
    tokens = {"input": 0, "output": 0}
    started = time.time()

    for step in range(max_steps):
        # On the last allowed step, force a conclusion by offering only submit_diagnosis.
        offered = tools if step < max_steps - 1 else [SUBMIT_DIAGNOSIS]
        resp = model.complete(SYSTEM_PROMPT, messages, offered, model_name)
        if resp.stop_reason == "error":
            return _report({"incident": True, "root_cause": f"Diagnosis could not complete: {resp.text}",
                            "recommended_fix": "Retry; verify ANTHROPIC_API_KEY and the model id.",
                            "confidence": "low", "evidence": [resp.text]},
                           trace, tokens, step + 1, started, backend, model_name)
        tokens["input"] += resp.usage["input_tokens"]
        tokens["output"] += resp.usage["output_tokens"]
        messages.append({"role": "assistant", "content": resp.content})

        submit = next((c for c in resp.tool_calls if c["name"] == "submit_diagnosis"), None)
        if submit:
            return _report(submit["input"], trace, tokens, step + 1, started, backend, model_name)

        if not resp.tool_calls:  # model replied with prose but no tool call — nudge it
            messages.append({"role": "user", "content": "Continue investigating, then call submit_diagnosis."})
            continue

        results = []
        for call in resp.tool_calls:
            out = call_tool(call["name"], **call["input"])
            trace.append({"step": step + 1, "tool": call["name"], "input": call["input"],
                          "result_summary": _summarize(out)})
            results.append({"type": "tool_result", "tool_use_id": call["id"], "content": json.dumps(out)})
        messages.append({"role": "user", "content": results})

    # Exhausted steps without a diagnosis (rare): return a low-confidence stub.
    return _report({"incident": True, "root_cause": "Inconclusive within step budget.",
                    "recommended_fix": "Investigate manually.", "confidence": "low", "evidence": []},
                   trace, tokens, max_steps, started, backend, model_name)


def _report(diag, trace, tokens, steps, started, backend, model_name) -> dict:
    return {**diag, "investigation": trace, "steps": steps,
            "tokens": tokens, "elapsed_s": round(time.time() - started, 2),
            "backend": backend, "model": model_name if backend == "anthropic" else "mock"}


# --- triage stage + cascade --------------------------------------------------

def gather_snapshot() -> dict:
    """Cheap, deterministic snapshot for triage (no model — tools are free)."""
    svc = call_tool("list_services")
    met = call_tool("get_metrics")
    metrics = met.get("metrics") if met.get("ok") else {"reachable": met.get("reachable", False)}
    return {
        "services": [{"service": s["service"], "running": s["running"], "status": s["status"]}
                     for s in svc.get("services", [])],
        "metrics": metrics,
    }


def triage(model, snapshot, model_name=TRIAGE_MODEL) -> dict:
    msg = (f"System snapshot (JSON):\n{json.dumps(snapshot)}\n\n"
           "Decide whether to escalate, then call triage_verdict.")
    resp = model.complete(TRIAGE_SYSTEM, [{"role": "user", "content": msg}], [TRIAGE_VERDICT],
                          model_name, tool_choice={"type": "tool", "name": "triage_verdict"})
    verdict = next((c["input"] for c in resp.tool_calls if c["name"] == "triage_verdict"),
                   {"incident": True, "confidence": "low", "reason": "no verdict returned; escalating"})
    tks = {"input": resp.usage["input_tokens"], "output": resp.usage["output_tokens"]}
    return {**verdict, "tokens": tks, "cost": cost(tks, model_name)}


def run_cascade(use_triage: bool = True) -> dict:
    """Triage (cheap) gates diagnosis (strong). Returns a combined report with costs."""
    model, backend = _make_model()

    if not use_triage:
        d = diagnose()
        dcost = cost(d["tokens"], DIAGNOSE_MODEL)
        return {"triage": None, "escalated": True, "diagnosis": d, "backend": backend,
                "cost": {"triage_usd": 0.0, "diagnosis_usd": dcost, "total_usd": dcost}}

    t = triage(model, gather_snapshot())
    escalate = t["incident"] or t["confidence"] == "low"
    d = diagnose() if escalate else None
    dcost = cost(d["tokens"], DIAGNOSE_MODEL) if d else 0.0
    return {"triage": t, "escalated": escalate, "diagnosis": d, "backend": backend,
            "cost": {"triage_usd": t["cost"], "diagnosis_usd": dcost,
                     "total_usd": round(t["cost"] + dcost, 6)}}


def _print_report(r: dict) -> None:
    verdict = "INCIDENT" if r["incident"] else "ALL CLEAR"
    print(f"\n=== {verdict}  (confidence: {r['confidence']}) ===")
    print(f"root cause : {r['root_cause']}")
    print(f"fix        : {r['recommended_fix']}")
    print("evidence   :")
    for e in r["evidence"]:
        print(f"  - {e}")
    print(f"\ninvestigation ({r['steps']} steps, {r['backend']} backend):")
    for t in r["investigation"]:
        print(f"  [{t['step']}] {t['tool']}({t['input'] or ''}) -> {t['result_summary']}")
    print(f"\ntokens: in={r['tokens']['input']} out={r['tokens']['output']}  |  {r['elapsed_s']}s")


def _print_cascade(c: dict) -> None:
    t = c["triage"]
    if t is not None:
        decision = "ESCALATE" if t["incident"] else "quiet"
        print(f"\n--- TRIAGE ({decision}, confidence: {t['confidence']}) ---")
        print(f"reason: {t['reason']}")
    if c["diagnosis"] is not None:
        _print_report(c["diagnosis"])
    elif not c["escalated"]:
        print("\n=== ALL CLEAR (triage) — diagnosis skipped, expensive loop avoided ===")
    cst = c["cost"]
    print(f"\ncost: triage=${cst['triage_usd']:.6f}  diagnosis=${cst['diagnosis_usd']:.6f}  "
          f"total=${cst['total_usd']:.6f}   [{c['backend']} backend]")


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="agent.diagnose")
    parser.add_argument("--no-triage", action="store_true",
                        help="skip triage and run the strong diagnostician directly (Day 4/5 behavior)")
    args = parser.parse_args(argv)
    _print_cascade(run_cascade(use_triage=not args.no_triage))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
