"""Eval harness: run every scenario, score the agent against ground truth, log the numbers.

Step 1 (this file): detection rate, false-alarm rate, time-to-diagnose, cost, and a keyword-based
root-cause/fix check. A real LLM-judge + the cost-split baseline come next.

Run: python -m eval.harness
"""
from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

import requests

from agent import diagnose as agent
from faults import injector

RESULTS_PATH = Path(__file__).resolve().parent.parent / "results.md"
SETTLE_DEFAULT = 2  # seconds after inject before observing (dep monitor / metrics settle)

DOMAIN_KEYWORDS = ["postgres", "redis", "memory", "leak", "pool", "connection",
                   "latency", "slow", "error", "cache", "crash", "app"]


def _keyword_match(truth: str, pred: str) -> bool:
    """Cheap offline judge: does the diagnosis mention the ground truth's key nouns?"""
    truth, pred = truth.lower(), pred.lower()
    keys = [k for k in DOMAIN_KEYWORDS if k in truth]
    return any(k in pred for k in keys) if keys else False


def _wait_healthy(timeout: int = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(f"{injector.SYSTEM_BASE_URL}/health", timeout=3).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(1)


def _predicted_incident(report: dict) -> bool:
    if not report["escalated"]:
        return False
    return bool(report["diagnosis"] and report["diagnosis"]["incident"])


def run_one(sid: str, scenario: dict, all_scenarios: dict) -> dict:
    injector._admin("/admin/reset")
    injector._dispatch(scenario["inject"])
    if scenario.get("traffic"):
        injector.generate_load(scenario["traffic"])
    time.sleep(scenario.get("settle", SETTLE_DEFAULT))

    started = time.time()
    report = agent.run_cascade(use_triage=True)
    elapsed = round(time.time() - started, 2)

    injector.cmd_clear_all(all_scenarios)
    _wait_healthy()

    benign = bool(scenario.get("benign"))
    predicted = _predicted_incident(report)
    diag = report.get("diagnosis")
    cause_ok = fix_ok = None
    if not benign and predicted and diag:
        cause_ok = _keyword_match(scenario["root_cause"], diag["root_cause"])
        fix_ok = _keyword_match(scenario["correct_fix"], diag["recommended_fix"])

    return {
        "id": sid, "benign": benign, "expected_incident": not benign,
        "predicted_incident": predicted, "escalated": report["escalated"],
        "cause_ok": cause_ok, "fix_ok": fix_ok,
        "cost_usd": report["cost"]["total_usd"], "elapsed_s": elapsed,
        "root_cause": diag["root_cause"] if diag else "(triage: quiet)",
    }


def aggregate(rows: list[dict]) -> dict:
    faults = [r for r in rows if not r["benign"]]
    benigns = [r for r in rows if r["benign"]]
    detected = [r for r in faults if r["predicted_incident"]]

    def rate(sub, whole):
        return round(len(sub) / len(whole), 3) if whole else 0.0

    cause_hits = [r for r in detected if r["cause_ok"]]
    fix_hits = [r for r in detected if r["fix_ok"]]
    return {
        "detection_rate": rate(detected, faults),
        "false_alarm_rate": rate([r for r in benigns if r["predicted_incident"]], benigns),
        "root_cause_accuracy": rate(cause_hits, faults),
        "fix_accuracy": rate(fix_hits, faults),
        "mean_time_to_diagnose_s": round(sum(r["elapsed_s"] for r in faults) / len(faults), 2) if faults else 0.0,
        "avg_cost_usd": round(sum(r["cost_usd"] for r in rows) / len(rows), 6) if rows else 0.0,
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
        "n_faults": len(faults), "n_benign": len(benigns),
    }


def _write_results(agg: dict, rows: list[dict], backend: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"\n## Eval run {ts} — backend: {backend}\n",
             f"- detection rate: **{agg['detection_rate']:.0%}** ({agg['n_faults']} faults)",
             f"- false-alarm rate: **{agg['false_alarm_rate']:.0%}** ({agg['n_benign']} benign)",
             f"- root-cause accuracy: **{agg['root_cause_accuracy']:.0%}**",
             f"- fix accuracy: **{agg['fix_accuracy']:.0%}**",
             f"- mean time-to-diagnose: **{agg['mean_time_to_diagnose_s']}s**",
             f"- avg cost/scenario: **${agg['avg_cost_usd']:.6f}**  (total ${agg['total_cost_usd']:.6f})\n",
             "| scenario | expected | predicted | cause | fix | cost | time |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        exp = "incident" if r["expected_incident"] else "benign"
        pred = "incident" if r["predicted_incident"] else "quiet"
        mark = lambda v: "—" if v is None else ("✓" if v else "✗")
        lines.append(f"| {r['id']} | {exp} | {pred} | {mark(r['cause_ok'])} | {mark(r['fix_ok'])} "
                     f"| ${r['cost_usd']:.6f} | {r['elapsed_s']}s |")
    RESULTS_PATH.write_text((RESULTS_PATH.read_text() if RESULTS_PATH.exists() else
                             "# Eval results\n") + "\n".join(lines) + "\n")


def main() -> int:
    scenarios = injector.load_scenarios()
    _, backend = agent._make_model()
    print(f"running {len(scenarios)} scenarios (backend: {backend})...")
    _wait_healthy()

    rows = []
    for sid, scenario in scenarios.items():
        print(f"  {sid} ...", end=" ", flush=True)
        r = run_one(sid, scenario, scenarios)
        ok = (r["predicted_incident"] == r["expected_incident"])
        print("ok" if ok else "MISS", f"(${r['cost_usd']:.4f}, {r['elapsed_s']}s)")
        rows.append(r)

    agg = aggregate(rows)
    _write_results(agg, rows, backend)
    print(f"\ndetection={agg['detection_rate']:.0%}  false_alarm={agg['false_alarm_rate']:.0%}  "
          f"root_cause={agg['root_cause_accuracy']:.0%}  MTTD={agg['mean_time_to_diagnose_s']}s")
    print(f"wrote {RESULTS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
