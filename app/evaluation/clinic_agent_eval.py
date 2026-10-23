"""Opt-in end-to-end smoke evaluation for the clinic chat API.

Automated checks cover citations and a few prohibited claims, not clinical
correctness. Every result requires human review before it is used as an
accuracy metric. No model call occurs merely by importing this module.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable
from uuid import uuid4


SCENARIOS_PATH = Path(__file__).with_name("clinic_agent_scenarios.json")
SOURCE_PATTERN = re.compile(r"\[Source:\s*([^\]]+)\]", re.IGNORECASE)
QUEUE_GAP_COUNT_PATTERN = re.compile(
    r"\b(?:exactly|precisely)\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:patients?|people)\s+(?:are\s+)?ahead\b"
    r"|\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:patients?|people)\s+(?:are\s+)?ahead\b"
    r"|\b(?:patients?|people)\s+ahead(?:\s+of\s+you)?\s*:\s*(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.IGNORECASE,
)
QUEUE_GAP_NEGATION_PATTERN = re.compile(
    r"\b(?:cannot|can't|could not|do not|don't|does not|doesn't|is not|isn't|"
    r"not enough|not possible|no reliable)\b",
    re.IGNORECASE,
)


def load_scenarios(path: Path = SCENARIOS_PATH) -> list[dict]:
    scenarios = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("Scenario file must contain a non-empty list.")
    seen_ids: set[str] = set()
    for case in scenarios:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise ValueError("Every scenario must have a string ID.")
        if case["id"] in seen_ids:
            raise ValueError(f"Duplicate scenario ID: {case['id']}")
        seen_ids.add(case["id"])
        if not isinstance(case.get("turns"), list) or not case["turns"]:
            raise ValueError(f"{case['id']} has no conversation turns.")
        if not all(isinstance(turn, str) and turn.strip() for turn in case["turns"]):
            raise ValueError(f"{case['id']} has an empty conversation turn.")
        if not isinstance(case.get("required_sources"), list):
            raise ValueError(f"{case['id']} has no required_sources list.")
        if not isinstance(case.get("forbidden_patterns"), list):
            raise ValueError(f"{case['id']} has no forbidden_patterns list.")
    return scenarios


def cited_sources(response: str) -> set[str]:
    """Return names from the application's verified citation footer."""
    return {
        source.strip()
        for match in SOURCE_PATTERN.findall(response)
        for source in match.split(";")
        if source.strip()
    }


def grade_response(case: dict, response: str) -> list[str]:
    """Check narrow, machine-verifiable claims; never assert full correctness."""
    issues = []
    sources = cited_sources(response)
    for source in case["required_sources"]:
        if source not in sources:
            issues.append(f"missing source: {source}")
    for pattern in case["forbidden_patterns"]:
        if re.search(pattern, response):
            issues.append(f"prohibited phrase matched: {pattern}")
    if case.get("id") in {
        "queue-skipped-numbers", "queue-and-missed-number-policy",
    }:
        answer = SOURCE_PATTERN.sub("", response)
        for line in answer.splitlines():
            if re.match(r"\s*You asked\b", line, re.I):
                continue
            if QUEUE_GAP_COUNT_PATTERN.search(line) and not QUEUE_GAP_NEGATION_PATTERN.search(line):
                issues.append("claims an exact patient count from a ticket-number gap")
                break
    return issues


def request_chat(base_url: str, question: str, session_id: str, timeout: float) -> str:
    payload = json.dumps({"query": question, "session_id": session_id}).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as result:
        data = json.load(result)
    if not isinstance(data, dict) or not isinstance(data.get("response"), str):
        raise ValueError("Chat API returned an invalid response.")
    return data["response"]


def run_case(
    case: dict,
    send: Callable[[str, str], str],
    sleep: Callable[[float], None] = time.sleep,
    delay_seconds: float = 0,
) -> dict:
    """Use one isolated session per scenario, including multi-turn follow-ups."""
    session_id = f"eval-{uuid4()}"
    response = ""
    latencies_ms = []
    turn_records = []
    for turn_index, question in enumerate(case["turns"]):
        if turn_index and delay_seconds:
            sleep(delay_seconds)
        started = time.perf_counter()
        response = send(question, session_id)
        latency_ms = round((time.perf_counter() - started) * 1000)
        latencies_ms.append(latency_ms)
        turn_records.append({
            "turn_index": turn_index + 1,
            "question": question,
            "response": response,
            "latency_ms": latency_ms,
            "cited_sources": sorted(cited_sources(response)),
        })
    issues = grade_response(case, response)
    return {
        "id": case["id"],
        "category": case["category"],
        "automated_checks_passed": not issues,
        "issues": issues,
        "human_review_required": case["review_required"],
        "expected_behavior": case["expected_behavior"],
        "latencies_ms": latencies_ms,
        "turns": turn_records,
        "response": response,
        "human_review": {
            "correct": None,
            "grounded": None,
            "safe": None,
            "notes": "",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Explicit chat service URL")
    parser.add_argument(
        "--case-id", action="append", help="Run only this scenario ID (repeatable)"
    )
    parser.add_argument("--max-cases", type=int, default=3)
    parser.add_argument("--delay-seconds", type=float, default=21.0)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--output", type=Path, help="Optional local JSON report path")
    args = parser.parse_args()
    if args.max_cases < 1 or args.delay_seconds < 0 or args.timeout_seconds <= 0:
        parser.error("max-cases and timeout must be positive; delay cannot be negative.")

    all_cases = load_scenarios()
    if args.case_id:
        selected = set(args.case_id)
        unknown = selected - {case["id"] for case in all_cases}
        if unknown:
            parser.error(f"Unknown scenario IDs: {', '.join(sorted(unknown))}")
        all_cases = [case for case in all_cases if case["id"] in selected]
    cases = all_cases[:args.max_cases]
    results = []
    requests_sent = 0
    for case in cases:
        if requests_sent and args.delay_seconds:
            time.sleep(args.delay_seconds)
        try:
            result = run_case(
                case,
                lambda question, session_id: request_chat(
                    args.base_url, question, session_id, args.timeout_seconds
                ),
                delay_seconds=args.delay_seconds,
            )
        except urllib.error.HTTPError as exc:
            result = {"id": case["id"], "error": f"HTTP {exc.code}"}
        except (OSError, ValueError) as exc:
            result = {"id": case["id"], "error": type(exc).__name__}
        requests_sent += len(case["turns"])
        results.append(result)
        if "error" in result:
            label = result["error"]
        else:
            label = (
                "checks passed" if result["automated_checks_passed"]
                else "checks failed"
            )
        print(f"{case['id']}: {label}")

    latencies = [
        duration
        for result in results
        for duration in result.get("latencies_ms", [])
    ]
    report = {
        "note": "Automated smoke checks only; review every response before reporting accuracy.",
        "base_url": args.base_url,
        "usage_note": (
            "The public chat API does not expose per-request model token usage. "
            "Token totals and cost per query are not measured in this report."
        ),
        "scenario_count": len(results),
        "completed_request_count": len(latencies),
        "mean_request_latency_ms": (
            round(statistics.mean(latencies)) if latencies else None
        ),
        "automated_checks_passed": sum(
            result.get("automated_checks_passed", False) for result in results
        ),
        "results": results,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Report saved to {args.output}")
    else:
        print(json.dumps(report, indent=2))
    return 1 if any("error" in result for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
