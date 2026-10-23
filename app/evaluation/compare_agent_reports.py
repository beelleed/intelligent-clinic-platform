"""Compare two locally saved clinic-agent evaluation reports.

This is a descriptive comparison, not an accuracy or cost benchmark.
No network or model calls are made.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path



def summarize_usage(records: list[dict]) -> dict:
    """Keep provider token totals unknown when any call lacks usage metadata."""
    providers: dict[str, dict] = {}
    for record in records:
        provider = record["provider"]
        item = providers.setdefault(provider, {
            "model_calls": 0, "usage_reported_calls": 0,
            "input_tokens": 0, "output_tokens": 0,
        })
        item["model_calls"] += 1
        if isinstance(record.get("input_tokens"), int) and isinstance(
            record.get("output_tokens"), int
        ):
            item["usage_reported_calls"] += 1
            item["input_tokens"] += record["input_tokens"]
            item["output_tokens"] += record["output_tokens"]
    for item in providers.values():
        if item["usage_reported_calls"] != item["model_calls"]:
            item["input_tokens"] = None
            item["output_tokens"] = None
    return providers


def load_report(path: Path) -> dict:
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or not isinstance(report.get("results"), list):
        raise ValueError(f"Invalid evaluation report: {path}")
    return report


def compare_reports(single: dict, supervisor: dict) -> dict:
    """Align successful cases by ID and preserve review uncertainty."""
    single_cases = {
        item["id"]: item for item in single["results"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    supervisor_cases = {
        item["id"]: item for item in supervisor["results"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    shared_ids = [case_id for case_id in single_cases if case_id in supervisor_cases]
    cases = []
    for case_id in shared_ids:
        left = single_cases[case_id]
        right = supervisor_cases[case_id]
        if "error" in left or "error" in right:
            cases.append({
                "id": case_id,
                "status": "incomplete",
                "single_error": left.get("error"),
                "supervisor_error": right.get("error"),
            })
            continue
        left_latencies = left.get("latencies_ms", [])
        right_latencies = right.get("latencies_ms", [])
        if not isinstance(left_latencies, list) or not isinstance(right_latencies, list):
            raise ValueError(f"Invalid latency data for scenario: {case_id}")
        left_usage = [
            usage for turn in left.get("turns", [])
            for usage in turn.get("model_usage", [])
        ]
        right_usage = [
            usage for turn in right.get("turns", [])
            for usage in turn.get("model_usage", [])
        ]
        cases.append({
            "id": case_id,
            "status": "compared",
            "single_total_latency_ms": sum(left_latencies),
            "supervisor_total_latency_ms": sum(right_latencies),
            "single_automated_checks_passed": left.get("automated_checks_passed"),
            "supervisor_automated_checks_passed": right.get("automated_checks_passed"),
            "single_human_review": left.get("human_review"),
            "supervisor_human_review": right.get("human_review"),
            "single_usage_by_provider": summarize_usage(left_usage),
            "supervisor_usage_by_provider": summarize_usage(right_usage),
        })

    compared = [case for case in cases if case["status"] == "compared"]
    return {
        "note": (
            "Descriptive paired comparison only. Automated source/phrase checks "
            "are not accuracy or safety scores; human review is required."
        ),
        "usage_note": (
            "Model tokens appear only for local reports with provider-reported "
            "usage. Cost in dollars remains unavailable without verified rates."
        ),
        "shared_case_count": len(shared_ids),
        "compared_case_count": len(compared),
        "mean_case_latency_ms": {
            "single": round(statistics.mean(
                case["single_total_latency_ms"] for case in compared
            )) if compared else None,
            "supervisor": round(statistics.mean(
                case["supervisor_total_latency_ms"] for case in compared
            )) if compared else None,
        },
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--single", required=True, type=Path)
    parser.add_argument("--supervisor", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = compare_reports(
            load_report(args.single), load_report(args.supervisor)
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Comparison saved to {args.output}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
