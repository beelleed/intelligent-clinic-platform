import json
import re
import sys
import urllib.error

import pytest

from app.evaluation import clinic_agent_eval
from app.evaluation.compare_agent_reports import compare_reports
from app.evaluation.run_local_comparison import summarize_usage
from app.evaluation.clinic_agent_eval import (
    cited_sources,
    grade_response,
    load_scenarios,
    run_case,
)


def test_scenario_set_covers_operational_and_safety_cases() -> None:
    cases = load_scenarios()
    assert len(cases) >= 10
    assert len({case["id"] for case in cases}) == len(cases)
    categories = {case["category"] for case in cases}
    assert {"live_operations", "known_product_gap", "multi_source",
            "memory", "medical_boundary", "privacy", "adversarial"} <= categories
    assert all(case["review_required"] for case in cases)


def test_source_footer_and_prohibited_claim_checks() -> None:
    case = {
        "required_sources": ["clinic_operations_get_queue_status"],
        "forbidden_patterns": [r"(?i)guaranteed wait"],
    }
    answer = "Serving 18.\n\n[Source: clinic_operations_get_queue_status; queue-policy]"
    assert cited_sources(answer) == {"clinic_operations_get_queue_status", "queue-policy"}
    assert grade_response(case, answer) == []
    assert len(grade_response(case, "Guaranteed wait of 36 minutes.")) == 2


def test_queue_gap_check_distinguishes_claim_from_negated_explanation() -> None:
    case = next(
        case for case in load_scenarios()
        if case["id"] == "queue-skipped-numbers"
    )
    source = "\n[Source: clinic_operations_get_queue_status]"
    wrong = "Yes. Patients ahead of you: 3" + source
    cautious = "I cannot prove that exactly three patients are ahead of you." + source
    wrong_two = "That is exactly two patients ahead of you, not three." + source
    echoed = (
        "You asked to prove exactly three patients are ahead of you.\n"
        "I cannot confirm that exactly three patients are ahead of you." + source
    )

    assert "claims an exact patient count" in " ".join(grade_response(case, wrong))
    assert "claims an exact patient count" in " ".join(grade_response(case, wrong_two))
    assert grade_response(case, cautious) == []
    assert grade_response(case, echoed) == []


def test_mixed_queue_policy_scenario_rejects_unsupported_count() -> None:
    case = next(
        case for case in load_scenarios()
        if case["id"] == "queue-and-missed-number-policy"
    )
    source = (
        "\n[Source: clinic_operations_get_queue_status; "
        "clinic_knowledge_read_knowledge_document; queue-and-wait-times]"
    )
    wrong = "There are 3 patients ahead of you. Ask the front desk." + source
    safe = "The count is unknown. Ask the front desk if you miss your number." + source
    assert "claims an exact patient count" in " ".join(grade_response(case, wrong))
    assert grade_response(case, safe) == []


def test_unissued_ticket_check_rejects_in_queue_claim() -> None:
    case = next(case for case in load_scenarios() if case["id"] == "queue-unissued")
    response = (
        "Ticket 28 is not issued, but patient_number 28 is in the queue."
        "\n[Source: clinic_operations_get_queue_status]"
    )
    assert grade_response(case, response)


def test_followup_uses_same_session_and_grades_final_answer() -> None:
    case = next(case for case in load_scenarios() if case["id"] == "procedure-followup")
    calls = []

    def send(question: str, session_id: str) -> str:
        calls.append((question, session_id))
        return "Estimated remaining time.\n\n[Source: clinic_operations_get_procedure_status]"

    result = run_case(case, send)
    assert len(calls) == 2
    assert calls[0][1] == calls[1][1]
    assert calls[0][1].startswith("eval-")
    assert result["automated_checks_passed"] is True
    assert result["human_review_required"] is True
    assert len(result["latencies_ms"]) == 2
    assert [turn["question"] for turn in result["turns"]] == case["turns"]
    assert all(turn["cited_sources"] == [
        "clinic_operations_get_procedure_status"
    ] for turn in result["turns"])
    assert result["human_review"] == {
        "correct": None, "grounded": None, "safe": None, "notes": "",
    }


def test_paired_comparison_excludes_errors_and_does_not_claim_accuracy() -> None:
    single = {"results": [
        {"id": "queue-current", "latencies_ms": [100],
         "automated_checks_passed": True, "human_review": {"correct": None}},
        {"id": "specialty-directory", "error": "HTTP 503"},
    ]}
    supervisor = {"results": [
        {"id": "queue-current", "latencies_ms": [250],
         "automated_checks_passed": False, "human_review": {"correct": None}},
        {"id": "specialty-directory", "latencies_ms": [300]},
    ]}
    comparison = compare_reports(single, supervisor)
    assert comparison["shared_case_count"] == 2
    assert comparison["compared_case_count"] == 1
    assert comparison["mean_case_latency_ms"] == {
        "single": 100, "supervisor": 250,
    }
    assert comparison["cases"][1]["status"] == "incomplete"
    assert "not accuracy" in comparison["note"]
    assert "Cost in dollars remains unavailable" in comparison["usage_note"]


def test_usage_summary_keeps_missing_provider_tokens_unknown() -> None:
    records = [
        {"provider": "openai", "input_tokens": 40, "output_tokens": 8},
        {"provider": "openai", "input_tokens": 30, "output_tokens": 5},
        {"provider": "nvidia", "input_tokens": None, "output_tokens": None},
    ]
    summary = summarize_usage(records)
    assert summary["openai"] == {
        "model_calls": 2, "usage_reported_calls": 2,
        "input_tokens": 70, "output_tokens": 13,
    }
    assert summary["nvidia"] == {
        "model_calls": 1, "usage_reported_calls": 0,
        "input_tokens": None, "output_tokens": None,
    }


def test_comparison_includes_local_usage_by_provider() -> None:
    base = {
        "id": "clinic-hours", "latencies_ms": [100],
        "turns": [{"model_usage": [
            {"provider": "openai", "input_tokens": 20, "output_tokens": 4}
        ]}],
    }
    other = {
        "id": "clinic-hours", "latencies_ms": [150],
        "turns": [{"model_usage": [
            {"provider": "nvidia", "input_tokens": 25, "output_tokens": 5}
        ]}],
    }
    result = compare_reports({"results": [base]}, {"results": [other]})
    case = result["cases"][0]
    assert case["single_usage_by_provider"]["openai"]["input_tokens"] == 20
    assert case["supervisor_usage_by_provider"]["nvidia"]["output_tokens"] == 5


def test_booking_check_does_not_flag_a_negated_explanation() -> None:
    case = next(
        case for case in load_scenarios()
        if case["id"] == "specialty-appointment-and-policy"
    )
    safe = (
        "A listed slot does not automatically mean you are booked. "
        "Once you reserve, you may receive a confirmation indicating "
        "your appointment is confirmed."
    )
    assert not any(
        re.search(pattern, safe)
        for pattern in case["forbidden_patterns"]
    )
    assert any(
        re.search(pattern, "I have booked your appointment.")
        for pattern in case["forbidden_patterns"]
    )
    assert any(
        re.search(pattern, "Listing or reserving an available slot would secure it.")
        for pattern in case["forbidden_patterns"]
    )


def test_duplicate_scenario_ids_are_rejected(tmp_path) -> None:
    case = next(case for case in load_scenarios() if case["id"] == "queue-current")
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps([case, case]), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate scenario ID"):
        load_scenarios(path)


def test_cli_reports_http_error_without_crashing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys, "argv",
        ["clinic_agent_eval", "--base-url", "http://127.0.0.1:8080",
         "--case-id", "queue-current", "--max-cases", "1",
         "--delay-seconds", "0"],
    )

    def unavailable(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "http://127.0.0.1:8080/chat", 503, "Unavailable", {}, None
        )

    monkeypatch.setattr(clinic_agent_eval, "request_chat", unavailable)
    assert clinic_agent_eval.main() == 1
    assert "queue-current: HTTP 503" in capsys.readouterr().out


def test_cli_can_select_one_scenario(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys, "argv",
        ["clinic_agent_eval", "--base-url", "http://127.0.0.1:8080",
         "--case-id", "specialty-directory", "--delay-seconds", "0"],
    )
    monkeypatch.setattr(
        clinic_agent_eval, "request_chat",
        lambda *_args: (
            "Doctors and schedules.\n\n[Source: clinic_operations_list_doctors; "
            "clinic_operations_get_doctor_schedule]"
        ),
    )
    assert clinic_agent_eval.main() == 0
    output = capsys.readouterr().out
    assert "specialty-directory: checks passed" in output
    assert "queue-current:" not in output
