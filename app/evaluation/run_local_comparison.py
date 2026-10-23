"""Run a small, opt-in paired evaluation against local clinic agent graphs.

This makes paid provider calls only with --execute. It never uses the public
chat endpoint, so public per-IP quotas are not consumed. Results require
human review and do not establish clinical accuracy or dollar cost.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from pathlib import Path
from uuid import uuid4

from app.evaluation.clinic_agent_eval import cited_sources, grade_response, load_scenarios
from app.evaluation.compare_agent_reports import summarize_usage
from app.services.chat import (
    create_chat_response,
    initialize_chat_runtime,
    shutdown_chat_runtime,
)


async def run_mode(
    mode: str, cases: list[dict], delay_seconds: float
) -> dict:
    os.environ["CLINIC_AGENT_MODE"] = mode
    os.environ["CLINIC_PROVIDER_ROUTING"] = (
        "domain" if mode == "supervisor" else "openai"
    )
    results = []
    all_usage = []
    completed_turns = 0
    await initialize_chat_runtime()
    try:
        for case in cases:
            session_id = f"paired-eval-{mode}-{uuid4()}"
            turns = []
            error = None
            for question in case["turns"]:
                if completed_turns and delay_seconds:
                    await asyncio.sleep(delay_seconds)
                usage = []
                started = time.perf_counter()
                try:
                    response = await create_chat_response(
                        question, session_id, usage_sink=usage
                    )
                except Exception as exc:
                    error = type(exc).__name__
                    break
                elapsed = round((time.perf_counter() - started) * 1000)
                completed_turns += 1
                all_usage.extend(usage)
                turns.append({
                    "question": question,
                    "response": response,
                    "latency_ms": elapsed,
                    "cited_sources": sorted(cited_sources(response)),
                    "model_usage": usage,
                })
            if error:
                results.append({"id": case["id"], "error": error, "turns": turns})
            else:
                issues = grade_response(case, turns[-1]["response"])
                results.append({
                    "id": case["id"],
                    "category": case["category"],
                    "expected_behavior": case["expected_behavior"],
                    "automated_checks_passed": not issues,
                    "issues": issues,
                    "latencies_ms": [turn["latency_ms"] for turn in turns],
                    "turns": turns,
                    "human_review": {
                        "correct": None, "grounded": None,
                        "safe": None, "notes": "",
                    },
                })
            print(f"{mode}: {case['id']}: {error or 'recorded'}", flush=True)
    finally:
        await shutdown_chat_runtime()
    latencies = [
        turn["latency_ms"] for result in results for turn in result["turns"]
    ]
    return {
        "mode": mode,
        "provider_routing": "domain" if mode == "supervisor" else "openai",
        "note": "Smoke checks are not accuracy or safety scores; human review required.",
        "usage_note": (
            "Token usage comes from model responses when reported. "
            "No dollar cost is calculated without verified provider rates."
        ),
        "scenario_count": len(results),
        "completed_request_count": completed_turns,
        "mean_request_latency_ms": round(statistics.mean(latencies)) if latencies else None,
        "usage_by_provider": summarize_usage(all_usage),
        "results": results,
    }


async def run_pair(cases: list[dict], delay_seconds: float) -> tuple[dict, dict]:
    previous = {
        name: os.environ.get(name)
        for name in ("CLINIC_AGENT_MODE", "CLINIC_PROVIDER_ROUTING")
    }
    try:
        single = await run_mode("single", cases, delay_seconds)
        supervisor = await run_mode("supervisor", cases, delay_seconds)
        return single, supervisor
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", action="append", required=True)
    parser.add_argument(
        "--mode", choices=("single", "supervisor", "both"), default="both",
        help="Run one graph for diagnostics or both graphs for a paired comparison",
    )
    parser.add_argument("--max-requests", type=int, default=6)
    parser.add_argument("--delay-seconds", type=float, default=2.0)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("data/agent-evals")
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Required to authorize actual OpenAI and NVIDIA model calls",
    )
    args = parser.parse_args()
    if args.max_requests < 1 or args.delay_seconds < 0:
        parser.error("max-requests must be positive and delay non-negative")
    all_cases = load_scenarios()
    selected = set(args.case_id)
    unknown = selected - {case["id"] for case in all_cases}
    if unknown:
        parser.error(f"Unknown scenario IDs: {', '.join(sorted(unknown))}")
    cases = [case for case in all_cases if case["id"] in selected]
    planned_requests = (2 if args.mode == "both" else 1) * sum(
        len(case["turns"]) for case in cases
    )
    if planned_requests > args.max_requests:
        parser.error(
            f"Paired run plans {planned_requests} requests; raise --max-requests "
            "only after checking your API budget."
        )
    if not args.execute:
        print(
            f"Dry run: {len(cases)} paired cases, up to {planned_requests} "
            "chat turns. Add --execute to call both providers."
        )
        return 0
    if args.mode == "both":
        single, supervisor = asyncio.run(run_pair(cases, args.delay_seconds))
        reports = (("single", single), ("supervisor", supervisor))
    else:
        reports = ((args.mode, asyncio.run(run_mode(
            args.mode, cases, args.delay_seconds
        ))),)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, report in reports:
        path = args.output_dir / f"{name}.json"
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Saved {name} report to {path}")
    return 1 if any(
        "error" in case for _, report in reports for case in report["results"]
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())
