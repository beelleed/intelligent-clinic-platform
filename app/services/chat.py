import asyncio
from contextvars import ContextVar
import json
import logging
import os
import re
import sys
import time
from uuid import uuid4
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from langchain.mcp import MCPAdapter
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool, tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from pydantic import ValidationError
from app.services.tavily import (
    GUIDANCE as EXTERNAL_GUIDANCE, TOOL_NAME as EXTERNAL_TOOL_NAME, TURN_BUDGET, append_external_sources,
    official_resource_search, tavily_enabled,
)


load_dotenv()


LOGGER = logging.getLogger(__name__)
PERFORMANCE_LOGGER = logging.getLogger("uvicorn.error")
_evaluation_usage_sink: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "clinic_evaluation_usage_sink", default=None
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MCP_CONFIG_PATH = PROJECT_ROOT / "mcp_config.json"
SYSTEM_PROMPT = """You are an intelligent clinic operations and knowledge assistant.

Follow these rules:
1. For clinic-specific facts, policies, schedules, queue status, procedure status,
   and appointments, use the available MCP tools. Treat tool results as the sole
   source of truth.
2. Never add causes, rules, warnings, promises, or recommended actions that are
   not explicitly supported by the tool results.
3. Do not write source citations yourself. The application adds verified tool
   and document citations after your answer.
4. For a ticket greater than last_issued_number, say it has not been issued
   in the synthetic snapshot. For a lower number, the snapshot cannot confirm
   individual issuance or calling order. Never calculate patients ahead,
   queue position, or waiting time from ticket-number gaps, even hypothetically.
5. If the tools do not provide enough information, say that the available clinic
   information is insufficient. Do not guess.
6. Do not provide medical diagnosis or treatment advice. Direct urgent medical
   concerns to qualified medical professionals or emergency services.
7. You may answer ordinary greetings and explain your capabilities without tools.
8. Operational and appointment tool timestamps use the timezone supplied in the
   tool result (normally America/Los_Angeles). Describe them as Pacific Time and
   do not relabel them as UTC.
9. When a question needs multiple independent tools, request all of those tool
   calls in the same response so they can run in parallel. Do not wait for one
   independent result before requesting the next tool.
10. Use only exact doctor_id values returned by the doctor directory, such as
    dr-lee. Never invent numeric IDs or specialty aliases. If a tool rejects an
    ID, retry with the exact directory ID; do not ask visitors for internal IDs.
    An empty appointment result for an unverified ID is not proof of no slots.
11. Viewing or listing an available slot NEVER books or holds it. Do not
    conflate listing with reserving. A public appointment request is not a
    confirmed clinic visit until the clinic sends confirmation. Never offer to
    reserve a slot unless the visitor explicitly asks to book one.
12. When reporting a recurring doctor schedule, explicitly say that it does
    not confirm the doctor's presence or location today. Current queue status
    and available appointment slots do not establish today's location; only a
    clinic-issued same-day update or a confirmed appointment location can do
    that. No available tool retrieves a doctor's actual live location. Do not
    offer to check live scheduling or infer today's location from queue or slot
    data. Refer visitors to the clinic for a same-day update instead.
13. If a visitor asks which specialties have doctors or appointments, list only
    the matching doctors. "Synthetic" describes this sample clinic data; it
    is not a separate specialty or service category. Do not reinterpret "A and
    B" as a request for one doctor who practices both specialties.
14. A returned available slot for a directory-verified doctor is evidence of
    a currently listed synthetic slot for that doctor's specialty. Do not
    undermine it with speculation about an unlisted service category. A listed
    slot is not a confirmed visit, and an appointment slot or queue snapshot
    cannot verify a doctor's location today.
End after answering the question. Do not add unsolicited follow-up offers,
especially offers to book a slot or retrieve a doctor's live location.
Keep answers concise, clear, and faithful to the retrieved evidence."""

SUPERVISOR_PROMPT = """You coordinate three specialist agents for a synthetic
multi-specialty outpatient clinic: policy, operations, and appointments.

Delegate clinic-specific questions to the relevant specialist or specialists.
For a question spanning several domains, consult each needed specialist and
combine only their verified findings. The specialists return their evidence
and limitations; they do not speak directly to the visitor. Do not invent a
doctor, policy, schedule, queue status, available slot, or booking. If evidence
is missing or inconsistent, state the uncertainty rather than guessing.
Specialist reports include raw verified_data from successful tools. Read those
results before composing your answer. If an appointment result contains slots,
state the actual doctor, dates, and times; do not say availability is unknown
or offer to fetch slots already retrieved. If a schedule contains locations,
state the actual location instead of claiming it is unavailable. Never infer
that a patient record, identity, or patient identification is needed here: this
prototype uses synthetic public data and anonymous booking references.
"Synthetic" describes the sample dataset, not a separate specialty. If the
directory identifies Dr. Rivera as Dermatology and her appointments tool lists
available slots, directly report Dermatology availability. Do not claim the
service is unconfirmed because there is no separate "synthetic Dermatology"
entry. Mention only doctors matching the requested specialties.
Queue status cannot establish appointment availability or near-term openings.
Do not suggest using it to infer bookable slots. Answer the question directly
and briefly; do not repeat the specialist reports or add speculative next steps.
When reporting a recurring schedule, explicitly state that it does not confirm
the doctor's presence or location today. Neither queue status nor available
slots verify today's location. Do not offer to check either for that purpose;
refer the visitor to a clinic-issued same-day update or their confirmed
appointment location if they need today's location. There is no tool for a
doctor's actual live location. Never offer to check or retrieve that location.
Operations owns doctor identities, queues, procedures, and recurring schedules.
Appointments owns current slot availability. Policy owns documented rules,
including whether viewing a slot constitutes a booking. A question combining
these facts needs all relevant specialists; policy alone cannot establish live
doctor or appointment facts. Use already retrieved specialist reports before
requesting another consultation.

The appointment specialist is read-only: this mode cannot book, cancel, or
reschedule a slot. Never offer to book, reserve, request, arrange, or confirm
an appointment on the visitor's behalf, even as an optional next step. If the
visitor asks to book, explain that this mode can only show available slots and
that they must use the clinic's separate request/confirmation process. Do not
claim that any reservation or staff update occurred.
End after the direct answer; do not append any "Would you like me to",
"If you want, I can", or similar follow-up offers.
Viewing or listing a slot never reserves it. Say this directly if asked.
Never infer actual patients ahead or waiting time from ticket-number gaps.
Do not provide diagnosis, individualized treatment, or medication dosing.
Urgent medical concerns require a qualified professional or emergency service.
The application adds verified citations; do not write source citations yourself.
Keep the final answer concise and explain when timestamps or estimates apply."""

SPECIALIST_PROMPTS = {
    "policy": """You are the clinic policy and RAG specialist. Use only clinic
knowledge MCP tools for documented public policies, specialties, locations,
check-in rules, and general guidance. Retrieve evidence before making clinic-
specific claims. If no relevant document is found, say so. Do not provide
medical advice or invent a policy. Do not write citations yourself.""",
    "operations": """You are the clinic operations specialist. Use only clinic
operations MCP tools for doctors, queues, procedures, and recurring schedules.
Use exact doctor_id values from the prefetched directory; never make up IDs.
When schedule entries are available, report the actual doctor, days, times, and
locations rather than offering to look them up later.
Check source timestamps and timezone. A recurring schedule does not confirm
today's presence or location. Say this explicitly when reporting recurring
schedules. Neither queue status nor available slots establish today's location;
do not offer to use them for that purpose. Do not infer actual patients ahead
or wait from ticket gaps,
or treat a procedure finish estimate as guaranteed. If data is absent, say so.
Do not write citations yourself.""",
    "appointments": """You are the read-only clinic appointments specialist. Use
the doctor directory to resolve a specialty or doctor name, then use available
appointment MCP tools to inspect synthetic slots. Use only exact directory IDs
as appointment filters; an invalid ID is not evidence that no slots exist.
When available slots are returned, report their actual doctor, date, and time;
do not say availability is unknown or offer to retrieve the same slots later.
Listing a slot is not a booking. You cannot reserve, cancel, or reschedule
appointments in this mode;
do not claim otherwise. Do not write citations yourself.""",
}


class LLMConfigurationError(RuntimeError):
    """Raised when required language-model configuration is missing."""


class LLMRequestError(RuntimeError):
    """Raised when a language-model request cannot produce a response."""


class MCPConnectionError(RuntimeError):
    """Raised when MCP tools cannot be discovered or reached."""


SOURCE_LINE_PATTERN = re.compile(r"(?im)^\s*\[?Sources?\s*:[^\n]*$")
INLINE_SOURCE_PATTERN = re.compile(r"(?i)\[Source\s*:[^\]\n]*\]")


def _document_ids(value: Any) -> list[str]:
    """Find document IDs in structured MCP output without trusting model text."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if isinstance(value, dict):
        found = []
        document_id = value.get("document_id")
        if isinstance(document_id, str) and document_id:
            found.append(document_id)
        for item in value.values():
            found.extend(_document_ids(item))
        return found
    if isinstance(value, list):
        return [document_id for item in value for document_id in _document_ids(item)]
    return []


def _verified_sources_for_messages(messages: list[Any]) -> list[str]:
    """Collect only successful, current-turn tool evidence, including specialists."""
    latest_question_index = max(
        (index for index, message in enumerate(messages)
         if isinstance(message, HumanMessage)),
        default=-1,
    )
    tool_messages = [
        message for message in messages[latest_question_index + 1:]
        if isinstance(message, ToolMessage) and message.status != "error"
    ]
    sources = []
    for message in tool_messages:
        if message.name == EXTERNAL_TOOL_NAME:
            try:
                payload = json.loads(message.content)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("results"):
                sources.append(EXTERNAL_TOOL_NAME)
            continue
        if message.name and message.name.startswith("consult_"):
            try:
                report = json.loads(message.content)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(report, dict):
                continue
            reported_sources = report.get("verified_sources", [])
            if isinstance(reported_sources, list):
                for source in reported_sources:
                    if isinstance(source, str) and source and source not in sources:
                        sources.append(source)
            continue
        if message.name and message.name.startswith("clinic_knowledge_"):
            document_ids = _document_ids(message.content)
            # A successful search with zero matches is not supporting evidence.
            if not document_ids:
                continue
            if message.name not in sources:
                sources.append(message.name)
            for document_id in document_ids:
                if document_id not in sources:
                    sources.append(document_id)
        elif message.name and message.name not in sources:
            sources.append(message.name)
    return sources


def _nested_evidence_objects(value: Any):
    """Decode structured MCP content, including nested specialist reports."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _nested_evidence_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nested_evidence_objects(child)


def _tool_evidence_objects(messages: list[Any], tool_name: str):
    latest_question_index = max(
        (index for index, item in enumerate(messages)
         if isinstance(item, HumanMessage)),
        default=-1,
    )
    for message in messages[latest_question_index + 1:]:
        if not isinstance(message, ToolMessage) or message.status == "error":
            continue
        if message.name == tool_name:
            yield from _nested_evidence_objects(message.content)
        elif message.name and message.name.startswith("consult_"):
            try:
                report = json.loads(message.content)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(report, dict):
                continue
            for item in report.get("verified_data", []):
                if isinstance(item, dict) and item.get("tool") == tool_name:
                    yield from _nested_evidence_objects(item.get("result"))


def _asks_missed_number_policy(question: str) -> bool:
    return bool(re.search(
        r"\bmiss(?:ed)?\b.{0,40}\b(?:number|ticket|turn)\b",
        question,
        re.I,
    ))


def _verified_queue_answer(messages: list[Any]) -> tuple[str, list[str]] | None:
    """Render queue facts and a verified missed-number rule without arithmetic."""
    question = next(
        (item.content for item in reversed(messages)
         if isinstance(item, HumanMessage) and isinstance(item.content, str)),
        "",
    )
    lower = question.lower()
    missed_number_policy = _asks_missed_number_policy(question)
    if not any(term in lower for term in ("queue", "ticket", "serving", "number", "wait")):
        return None
    if any(term in lower for term in (
        "appointment", "slot", "procedure", "surgery", "schedule", "location",
        "guarantee", "hours",
    )):
        return None
    if "policy" in lower and not missed_number_policy:
        return None

    snapshots = {}
    for obj in _tool_evidence_objects(messages, "clinic_operations_get_queue_status"):
        if not (
            isinstance(obj.get("doctor_id"), str)
            and isinstance(obj.get("doctor_name"), str)
            and type(obj.get("current_number")) is int
            and type(obj.get("last_issued_number")) is int
            and isinstance(obj.get("updated_at"), str)
        ):
            continue
        snapshots[obj["doctor_id"]] = obj
    if not snapshots:
        return None

    ticket_match = re.search(
        r"\b(?:ticket|number)\s*(?:number|#|is|:)?\s*(\d{1,4})\b"
        r"|\bI\s+hold\s+(\d{1,4})\b",
        question,
        re.I,
    )
    ticket_number = (
        int(next(value for value in ticket_match.groups() if value))
        if ticket_match else None
    )
    lines = ["Synthetic queue snapshot:"]
    for obj in snapshots.values():
        lines.append(
            f"- {obj['doctor_name']}: now serving number {obj['current_number']}; "
            f"last issued number {obj['last_issued_number']}; "
            f"updated {obj['updated_at']}."
        )
        if ticket_number is not None:
            if ticket_number > obj["last_issued_number"]:
                lines.append(
                    f"- Ticket {ticket_number} is above the last issued number "
                    "and has not been issued in this snapshot."
                )
            else:
                lines.append(
                    f"- This snapshot cannot confirm whether ticket "
                    f"{ticket_number} was individually issued or called."
                )
    lines.append(
        "Ticket numbers can be skipped or called out of order. Their difference "
        "does not tell us how many patients are ahead or how long the wait is."
    )
    sources = ["clinic_operations_get_queue_status"]
    if missed_number_policy:
        missed_rule = None
        for obj in _tool_evidence_objects(
            messages, "clinic_knowledge_read_knowledge_document",
        ):
            if obj.get("document_id") != "queue-and-wait-times":
                continue
            content = obj.get("content")
            if not isinstance(content, str):
                continue
            section = re.search(
                r"(?ims)^## Missed number\s*\n(.*?)(?=^## |\Z)", content,
            )
            if section and section.group(1).strip():
                missed_rule = " ".join(section.group(1).split())
                break
        if missed_rule:
            lines.append(f"Missed-number policy: {missed_rule}")
            sources.extend((
                "clinic_knowledge_read_knowledge_document",
                "queue-and-wait-times",
            ))
        else:
            lines.append(
                "The missed-number policy could not be verified from the "
                "available clinic sources. Please ask the front desk."
            )
    return "\n".join(lines), sources


def _verified_schedule_answer(answer: str, messages: list[Any]) -> str:
    """Render schedule-only questions from verified facts, without live claims."""
    question = next(
        (item.content for item in reversed(messages)
         if isinstance(item, HumanMessage) and isinstance(item.content, str)),
        "",
    )
    lower = question.lower()
    if not ("schedule" in lower or "normally work" in lower):
        return answer
    if any(term in lower for term in (
        "appointment", "slot", "availability", "queue", "ticket",
        "procedure", "surgery", "operation",
    )):
        return answer

    doctors = []
    for obj in _tool_evidence_objects(messages, "clinic_operations_list_doctors"):
        if isinstance(obj.get("doctors"), list):
            doctors = [
                doctor for doctor in obj["doctors"]
                if isinstance(doctor, dict)
                and all(isinstance(doctor.get(key), str)
                        for key in ("id", "name", "specialty"))
            ]
            break
    requested_ids = set(_mentioned_doctors(question, doctors))
    if not requested_ids:
        return answer

    schedules: dict[str, list[tuple[str, str, str, str]]] = {}
    for obj in _tool_evidence_objects(
        messages, "clinic_operations_get_doctor_schedule",
    ):
        doctor_id = obj.get("doctor_id")
        if doctor_id not in requested_ids or not isinstance(obj.get("schedule"), list):
            continue
        entries = schedules.setdefault(doctor_id, [])
        for entry in obj["schedule"]:
            if not isinstance(entry, dict) or not all(
                isinstance(entry.get(key), str) for key in (
                    "weekday", "start_time", "end_time", "location",
                )
            ):
                continue
            detail = (
                entry["weekday"], entry["start_time"], entry["end_time"],
                entry["location"],
            )
            if detail not in entries:
                entries.append(detail)
    if any(not schedules.get(doctor_id) for doctor_id in requested_ids):
        return answer

    lines = ["Recurring doctor schedules:"]
    for doctor in doctors:
        if doctor["id"] not in requested_ids:
            continue
        lines.append(f"- {doctor['name']} ({doctor['specialty']}):")
        for weekday, start, end, location in schedules[doctor["id"]]:
            lines.append(f"  - {weekday} {start}–{end} at {location}")
    lines.append(
        "These recurring schedules do not confirm a doctor's presence or "
        "location today. For same-day changes, contact the clinic directly."
    )
    return "\n".join(lines)


def _correct_missed_appointment_slots(answer: str, messages: list[Any]) -> str:
    """Use verified slot data if the model denies or omits retrieved slots."""
    question = next(
        (item.content for item in reversed(messages)
         if isinstance(item, HumanMessage) and isinstance(item.content, str)),
        "",
    )
    lower = question.lower()
    if not any(term in lower for term in ("appointment", "slot", "availability")):
        return answer

    doctors = []
    for obj in _tool_evidence_objects(messages, "clinic_operations_list_doctors"):
        if isinstance(obj.get("doctors"), list):
            doctors = [
                doctor for doctor in obj["doctors"]
                if isinstance(doctor, dict)
                and all(isinstance(doctor.get(key), str)
                        for key in ("id", "name", "specialty"))
            ]
            break
    requested_ids = set(_mentioned_doctors(question, doctors))
    if not requested_ids:
        return answer

    slots = {}
    availability_lists = []
    for obj in _tool_evidence_objects(
        messages, "clinic_appointments_list_available_appointments",
    ):
        if isinstance(obj.get("slots"), list):
            availability_lists.append(obj["slots"])
        if (
            obj.get("doctor_id") in requested_ids
            and all(isinstance(obj.get(key), str) for key in (
                "doctor_name", "appointment_date", "start_time", "end_time",
            ))
            and isinstance(obj.get("slot_id"), int)
        ):
            slots[obj["slot_id"]] = obj
    if not slots:
        if not availability_lists or any(availability_lists) or any(
            term in lower for term in (
                "queue", "ticket", "procedure", "surgery", "wait", "hours",
                "parking", "privacy", "cancel", "reschedule",
            )
        ):
            return answer
        lines = [
            "No available future appointment slots are listed in the current lookup.",
            "A doctor being listed does not mean an appointment is available.",
            "Doctors in the directory:",
        ]
        lines.extend(
            f"- {doctor['name']} ({doctor['specialty']})"
            for doctor in doctors if doctor["id"] in requested_ids
        )
    else:
        missing_slot = any(
            slot["appointment_date"] not in answer
            or slot["start_time"] not in answer
            for slot in slots.values()
        )
        contradicted = bool(re.search(
            r"(?i)(?:no explicit .{0,40}(?:entry|offering)|"
            r"(?:does not|cannot|can't) confirm .{0,60}(?:offering|availability)|"
            r"slots?.{0,35}(?:not provided|aren't provided))",
            answer,
        ))
        if not missing_slot and not contradicted:
            return answer
        lines = ["Available synthetic appointment slots:"]
        for slot in sorted(slots.values(), key=lambda item: (
            item["appointment_date"], item["start_time"], item["slot_id"],
        )):
            lines.append(
                f"- {slot['doctor_name']}: {slot['appointment_date']} "
                f"{slot['start_time']}–{slot['end_time']} Pacific Time "
                f"(slot ID {slot['slot_id']})."
            )

    if any(term in lower for term in ("where", "location", "normally work")):
        seen_schedule_details = set()
        for obj in _tool_evidence_objects(
            messages, "clinic_operations_get_doctor_schedule",
        ):
            if obj.get("doctor_id") not in requested_ids:
                continue
            entries = obj.get("schedule")
            if not isinstance(entries, list):
                continue
            details = []
            for entry in entries:
                if not isinstance(entry, dict) or not all(
                    isinstance(entry.get(key), str) for key in (
                        "weekday", "start_time", "end_time", "location",
                    )
                ):
                    continue
                key = (
                    obj["doctor_id"], entry["weekday"], entry["start_time"],
                    entry["end_time"], entry["location"],
                )
                if key in seen_schedule_details:
                    continue
                seen_schedule_details.add(key)
                details.append(
                    f"{entry['weekday']} {entry['start_time']}–{entry['end_time']} "
                    f"at {entry['location']}"
                )
            if details:
                lines.append(
                    f"{obj.get('doctor_name', 'The doctor')}'s recurring "
                    f"schedule: {'; '.join(details)}."
                )
    if any(term in lower for term in ("booked", "booking", "reserve", "confirm")):
        lines.append(
            "Viewing a slot does not book it. An appointment request is not "
            "confirmed until the clinic sends confirmation."
        )
    return "\n".join(lines)


def _verified_unknown_doctor_queue_answer(messages: list[Any]) -> str | None:
    """State directory absence without inventing a queue or a reason for it."""
    question = _latest_question({"messages": messages})
    lower = question.lower()
    if not any(term in lower for term in ("queue", "serving", "number", "ticket")):
        return None
    if any(term in lower for term in (
        "appointment", "slot", "procedure", "surgery", "policy", "hours",
        "schedule", "location",
    )):
        return None
    names = re.findall(
        r"\b(?i:dr)\.?\s+([A-Za-z][A-Za-z'-]*(?:[ \t]+[A-Z][a-z'-]+)?)",
        question,
    )
    if len(names) != 1:
        return None
    for obj in _tool_evidence_objects(messages, "clinic_operations_list_doctors"):
        doctors = obj.get("doctors")
        if not isinstance(doctors, list) or not all(
            isinstance(doctor, dict) and all(
                isinstance(doctor.get(key), str) and doctor[key].strip()
                for key in ("id", "name", "specialty")
            ) for doctor in doctors
        ):
            continue
        requested = names[0].casefold()
        for doctor in doctors:
            name = re.sub(r"^(?i:dr)\.?\s+", "", doctor["name"]).casefold()
            if requested == name or requested in name.split():
                return None
        lines = [
            f"Dr. {names[0]} is not listed in the verified clinic doctor directory.",
            "No queue number is available for that doctor.",
        ]
        if doctors:
            lines.append("Available doctors:")
            lines.extend(
                f"- {doctor['name']} ({doctor['specialty']})"
                for doctor in doctors
            )
        return "\n".join(lines) + "\n\n[Source: clinic_operations_list_doctors]"
    return None


def _answer_with_verified_sources(answer: str, messages: list[Any]) -> str:
    """Append citations from successful tools, never from the model's prose."""
    if _staff_procedure_write_requested(messages):
        return (
            "I cannot extend a procedure estimate or publish an update. "
            "This assistant has no staff update or publishing tools. "
            "No estimate was changed and no update was published. "
            "Please ask authorized clinic staff to make and confirm the change."
        )
    if _procedure_followup_question({"messages": messages}) is not None:
        expected_doctor = _previous_turn_procedures(messages)[0]["doctor_id"]
        snapshots = list(_tool_evidence_objects(
            messages, "clinic_operations_get_procedure_status",
        ))
        snapshot = next((item for item in reversed(snapshots) if (
            item.get("doctor_id") == expected_doctor
            and isinstance(item.get("doctor_name"), str)
            and isinstance(item.get("status"), str)
            and "estimated_remaining_minutes" in item
            and (type(item["estimated_remaining_minutes"]) is int
                 or item["estimated_remaining_minutes"] is None)
            and isinstance(item.get("updated_at"), str)
        )), None)
        if snapshot is None:
            return (
                "I could not verify refreshed procedure status from the current "
                "lookup. Please check with clinic staff."
            )
        remaining = snapshot["estimated_remaining_minutes"]
        remaining_line = (
            f"Estimated remaining time: {remaining} minutes."
            if remaining is not None else
            "No remaining-time estimate is provided in the refreshed status."
        )
        return (
            f"{snapshot['doctor_name']}'s refreshed procedure status: {snapshot['status']}.\n"
            f"{remaining_line}\n"
            f"Last source update: {snapshot['updated_at']}.\n"
            "Completion estimates are not guaranteed and can change.\n\n"
            "[Source: clinic_operations_get_procedure_status]"
        )
    unknown_doctor_answer = _verified_unknown_doctor_queue_answer(messages)
    if unknown_doctor_answer is not None:
        return unknown_doctor_answer
    queue_answer = _verified_queue_answer(messages)
    if queue_answer is not None:
        body, queue_sources = queue_answer
        return (
            f"{body}\n\n"
            f"[Source: {'; '.join(queue_sources)}]"
        )
    sources = _verified_sources_for_messages(messages)
    cleaned = SOURCE_LINE_PATTERN.sub("", answer)
    cleaned = INLINE_SOURCE_PATTERN.sub("", cleaned).strip()
    cleaned = _verified_schedule_answer(cleaned, messages)
    cleaned = _correct_missed_appointment_slots(cleaned, messages)
    if sources:
        # Optional next-step offers can contradict tool capabilities or imply
        # that queue/slot checks prove today's clinician location.
        cleaned = re.sub(
            r"(?i)\s+For the latest, I can [^.?!]*[.?!]", "", cleaned,
        )
        cleaned = re.sub(
            r"(?i)\s+If you(?:['\u2019]d| would)? like,\s*I can\b"
            r"[^.!?\n]*(?:[.!?]|$)",
            "", cleaned,
        )
        cleaned = re.sub(
            r"(?i)\s+If you need\b[^.!?\n]{0,180},\s*I can\b"
            r"[^.!?\n]*(?:[.!?]|$)",
            "", cleaned,
        )
        lines = []
        skip_offer_list = False
        for line in cleaned.splitlines():
            optional_offer = re.match(
                r"^\s*(?:[-*]\s*)?(?:Would you like me to\b|"
                r"If you(?:['\u2019]d| would)? like\b|If you want\b|"
                r"If you need\b.*\bI can\b)",
                line,
                re.I,
            )
            if optional_offer:
                skip_offer_list = line.rstrip().endswith(":")
                continue
            if skip_offer_list and (
                not line.strip() or re.match(r"^\s*[-*]\s+", line)
            ):
                continue
            skip_offer_list = False
            lines.append(line)
        cleaned = "\n".join(lines).strip()
    if "clinic_operations_get_doctor_schedule" in sources and not re.search(
        r"\b(?:does not|do not|doesn't|don't|cannot|can't)\b"
        r".{0,65}\b(?:confirm|guarantee|prove|verify)\b"
        r".{0,65}\b(?:today|current)\b",
        cleaned,
        re.I | re.S,
    ):
        cleaned += (
            "\n\nRecurring schedules do not confirm a doctor's presence "
            "or location today."
        )
    if not sources:
        return cleaned
    return f"{cleaned}\n\n[Source: {'; '.join(sources)}]"


def _required_environment_value(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise LLMConfigurationError(f"LLM configuration error: {name} is not set.")
    return value


def _create_model() -> ChatOpenAI:
    api_key = _required_environment_value("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "").strip() or "gpt-5-nano"
    reasoning_effort = (
        os.getenv("OPENAI_REASONING_EFFORT", "").strip() or "minimal"
    )
    base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None

    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        reasoning_effort=reasoning_effort,
    )


def _create_nvidia_model() -> ChatOpenAI:
    """Create the optional NVIDIA-hosted NIM model for policy questions."""
    api_key = _required_environment_value("NVIDIA_API_KEY")
    model_name = (
        os.getenv("NVIDIA_MODEL", "").strip()
        or "nvidia/nemotron-3.5-lightning-30b-a3b"
    )
    thinking = os.getenv("NVIDIA_ENABLE_THINKING", "false").strip().lower()
    if thinking not in {"true", "false"}:
        raise LLMConfigurationError(
            "LLM configuration error: NVIDIA_ENABLE_THINKING must be true or false."
        )
    return ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url="https://integrate.api.nvidia.com/v1",
        temperature=1,
        top_p=0.95,
        max_tokens=4096,
        extra_body={"chat_template_kwargs": {"enable_thinking": thinking == "true"}},
    )


def _mcp_config_path() -> Path:
    configured_path = os.getenv("MCP_SERVERS_CONFIG", "").strip()
    if not configured_path:
        return DEFAULT_MCP_CONFIG_PATH

    path = Path(configured_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _load_mcp_config() -> dict:
    """Load and minimally validate the configured MCP server definitions."""
    config_path = _mcp_config_path()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MCPConnectionError(
            "MCP configuration error: the server configuration cannot be loaded."
        ) from exc

    servers = config.get("mcpServers") if isinstance(config, dict) else None
    if not isinstance(servers, dict) or not servers:
        raise MCPConnectionError(
            "MCP configuration error: mcpServers must be a non-empty object."
        )

    for server_name, server_config in servers.items():
        if not isinstance(server_config, dict):
            raise MCPConnectionError(
                f"MCP configuration error: '{server_name}' must be an object."
            )
        if server_config.get("command") == "${PYTHON_EXECUTABLE}":
            server_config["command"] = sys.executable

    return config


async def _load_mcp_tools() -> list[BaseTool]:
    """Discover LangChain-compatible tools through the real MCP protocol."""
    try:
        async with MCPAdapter(_load_mcp_config()) as adapter:
            return await adapter.list_tools()
    except MCPConnectionError:
        raise
    except Exception as exc:
        LOGGER.error(
            "MCP tool discovery failed (error_type=%s)",
            type(exc).__name__,
        )
        raise MCPConnectionError(
            "MCP connection error: clinic tools are unavailable."
        ) from exc


def _latest_question(state: MessagesState) -> str:
    return next(
        (message.content for message in reversed(state["messages"])
         if isinstance(message, HumanMessage) and isinstance(message.content, str)),
        "",
    )


def _previous_turn_procedures(messages: list[Any]) -> list[dict[str, Any]]:
    human_indexes = [i for i, item in enumerate(messages) if isinstance(item, HumanMessage)]
    if len(human_indexes) < 2:
        return []
    previous_turn = messages[human_indexes[-2]:human_indexes[-1]]
    by_id = {}
    for item in _tool_evidence_objects(previous_turn, "clinic_operations_get_procedure_status"):
        if isinstance(item.get("doctor_id"), str) and isinstance(item.get("doctor_name"), str):
            by_id[item["doctor_id"]] = item
    return list(by_id.values())


def _procedure_followup_question(state: MessagesState) -> str | None:
    question = _latest_question(state)
    lower = question.lower()
    if re.search(r"\bdr\.?\s|\bdr-", lower) or any(
        word in lower for word in ("appointment", "queue", "ticket", "publish", "extend")
    ):
        return None
    refresh = any(term in lower for term in ("check again", "recheck", "refresh"))
    remaining = re.search(r"\b(?:remaining|remain|left)\b", lower) and any(
        term in lower for term in ("minute", "time", "long")
    )
    previous = _previous_turn_procedures(state["messages"])
    if not (refresh or remaining) or len(previous) != 1:
        return None
    doctor = previous[0]
    return (
        f"{question}\nRecheck current procedure status for "
        f"{doctor['doctor_name']} (doctor_id: {doctor['doctor_id']})."
    )


def _staff_procedure_write_requested(messages: list[Any]) -> bool:
    question = _latest_question({"messages": messages})
    lower = question.lower()
    action = re.search(
        r"^\s*(?:please\s+)?(?:extend|publish|change|update|modify)\b"
        r"|\b(?:can|could|would|will)\s+you\s+(?:please\s+)?"
        r"(?:extend|publish|change|update|modify)\b",
        lower,
    )
    return bool(action and (
        any(term in lower for term in ("procedure", "surgery"))
        or _previous_turn_procedures(messages)
    ))


def _doctor_question(question: str) -> bool:
    lower = question.lower()
    return bool(re.search(r"\bent\b", question, re.I)) or any(term in lower for term in (
        "doctor", "dr.", "specialty", "dermatology", "otolaryngology",
        "appointment", "slot", "schedule", "queue", "ticket", "procedure",
    ))


def _required_specialists(question: str) -> tuple[str, ...]:
    """Set a minimum evidence route; the manager may consult more specialists."""
    lower = question.lower()
    required = []
    if re.search(r"\bent\b", question, re.I) or any(term in lower for term in (
        "doctor", "dr.", "dermatology", "otolaryngology",
        "schedule", "queue", "ticket", "procedure", "where", "location",
    )):
        required.append("operations")
    if any(term in lower for term in (
        "appointment", "slot", "availability", "available to book",
    )):
        required.append("appointments")
    if _asks_missed_number_policy(question) or any(term in lower for term in (
        "policy", "rule", "guarantee", "booked", "booking", "confirmed",
        "check-in", "late", "parking", "hours", "privacy", "emergency",
    )):
        required.append("policy")
    return tuple(required)


def _supervisor_answer_ready_tools(question: str) -> tuple[str, ...]:
    """Avoid re-consulting agents after both queue and missed-policy facts arrive."""
    if _asks_missed_number_policy(question) and "operations" in _required_specialists(question):
        return ("consult_operations_agent", "consult_policy_agent")
    return (
        "consult_operations_agent", "consult_appointments_agent",
        "consult_policy_agent",
    )


def _exception_chain_contains(
    error: BaseException,
    error_types: tuple[type[BaseException], ...],
) -> bool:
    """Check wrapped exceptions without exposing their messages."""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        if isinstance(current, error_types):
            return True
        visited.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _mcp_tool_error_message(error: Exception) -> str:
    """Log safe diagnostics and return a model-visible MCP failure message."""
    error_type = type(error).__name__
    if _exception_chain_contains(
        error,
        (json.JSONDecodeError, ValidationError),
    ):
        LOGGER.error(
            "MCP tool returned an invalid response (error_type=%s)",
            error_type,
        )
        return "MCP tool returned an invalid or schema-incompatible response."

    LOGGER.error(
        "MCP tool execution failed (error_type=%s)",
        error_type,
    )
    return "MCP tool execution failed. Verify the request and try again."


def _build_agent_graph(
    model: BaseChatModel,
    tools: list[BaseTool],
    system_prompt: str,
    *,
    checkpointer: MemorySaver | None = None,
    parallel_tool_calls: bool | None = True,
    agent_name: str = "single",
    provider: str = "openai",
    prefetch: Any | None = None,
    answer_from_prefetched_tools: tuple[str, ...] = (),
    require_all_prefetched_tools: bool = False,
    answer_ready_tools_for_question: Callable[[str], tuple[str, ...]] | None = None,
):
    if tools:
        # Do not send provider-specific parallel-tool settings to NVIDIA NIM.
        model_with_tools = (
            model.bind_tools(tools, parallel_tool_calls=parallel_tool_calls)
            if isinstance(model, ChatOpenAI) and parallel_tool_calls is not None
            else model.bind_tools(tools)
        )
    else:
        model_with_tools = model

    async def call_model(state: MessagesState) -> dict[str, list[AIMessage]]:
        messages = [SystemMessage(content=system_prompt), *state["messages"]]
        started_at = time.perf_counter()
        prefetched_names = {
            item.name for item in state["messages"]
            if isinstance(item, ToolMessage) and item.status != "error"
        }
        # A specialist that already has the exact public evidence can make
        # one synthesis call without giving the model tools to re-fetch it.
        # Other cases retain normal tool choice. Keep model doubles on the
        # existing path so the tests exercise their simulated tool calls.
        expected_prefetched_names = set(
            answer_ready_tools_for_question(_latest_question(state))
            if answer_ready_tools_for_question is not None
            else answer_from_prefetched_tools
        )
        evidence_ready = (
            isinstance(model, BaseChatModel)
            and bool(expected_prefetched_names)
            and (
                expected_prefetched_names.issubset(prefetched_names)
                if require_all_prefetched_tools else
                bool(prefetched_names.intersection(expected_prefetched_names))
            )
        )
        message = await (
            model if evidence_ready else model_with_tools
        ).ainvoke(messages)
        usage_sink = _evaluation_usage_sink.get()
        if usage_sink is not None:
            usage = message.usage_metadata or {}
            usage_sink.append({
                "agent": agent_name,
                "provider": provider,
                "model": getattr(model, "model_name", None),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "input_token_details": usage.get("input_token_details"),
                "duration_ms": round((time.perf_counter() - started_at) * 1000),
            })
        PERFORMANCE_LOGGER.info(
            "Clinic model call completed "
            "(agent=%s, duration_ms=%d, message_count=%d, tool_call_count=%d)",
            agent_name,
            round((time.perf_counter() - started_at) * 1000),
            len(messages),
            len(message.tool_calls),
        )
        return {"messages": [message]}

    builder = StateGraph(MessagesState)
    builder.add_node("model", call_model)
    if prefetch is not None:
        builder.add_node("prefetch", prefetch)
        builder.add_edge(START, "prefetch")
        builder.add_edge("prefetch", "model")
    else:
        builder.add_edge(START, "model")

    if tools:
        builder.add_node(
            "tools",
            ToolNode(tools, handle_tool_errors=_mcp_tool_error_message),
        )
        builder.add_conditional_edges("model", tools_condition)
        builder.add_edge("tools", "model")

    return builder.compile(checkpointer=checkpointer)


chat_memory = MemorySaver()
supervisor_memory = MemorySaver()


def _doctors_from_directory(result: Any) -> list[dict[str, str]]:
    """Read only the structured doctor records returned by the MCP tool."""
    try:
        if isinstance(result, list):
            text_block = next(
                block["text"] for block in result
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
            result = json.loads(text_block)
        elif isinstance(result, str):
            result = json.loads(result)
    except (StopIteration, json.JSONDecodeError, KeyError):
        return []
    if not isinstance(result, dict) or not isinstance(result.get("doctors"), list):
        return []
    return [
        doctor for doctor in result["doctors"]
        if isinstance(doctor, dict)
        and isinstance(doctor.get("id"), str)
        and isinstance(doctor.get("name"), str)
        and isinstance(doctor.get("specialty"), str)
    ]


def _mentioned_doctors(question: str, doctors: list[dict[str, str]]) -> list[str]:
    lower = question.lower()
    selected = []
    for doctor in doctors:
        last_name = doctor["name"].split()[-1].lower()
        specialty = doctor["specialty"].lower()
        mentioned = (
            bool(re.search(rf"\b{re.escape(last_name)}\b", lower))
            or doctor["id"].lower() in lower
            or specialty in lower
            or ("(ent)" in specialty and bool(re.search(r"\bent\b", lower)))
        )
        if mentioned and doctor["id"] not in selected:
            selected.append(doctor["id"])
    return selected


def _single_evidence_prefetch(tools: list[BaseTool]):
    """Fetch routine, explicitly requested facts before asking the model to answer."""
    by_name = {item.name: item for item in tools}

    async def prefetch(state: MessagesState) -> dict[str, list[Any]]:
        question = _procedure_followup_question(state) or _latest_question(state)
        lower = question.lower()
        policy_terms = (
            "policy", "guarantee", "booked", "booking", "confirmed",
            "hours", "parking", "privacy", "contact", "check-in", "late",
        )
        if not _doctor_question(question) and not any(
            term in lower for term in policy_terms
        ) and not _asks_missed_number_policy(question):
            return {"messages": []}
        directory = by_name.get("clinic_operations_list_doctors")

        calls = []
        results = []

        async def fetch(name: str, args: dict[str, Any]) -> Any | None:
            tool = by_name.get(name)
            if tool is None:
                return None
            try:
                result = await tool.ainvoke(args)
            except Exception as exc:
                LOGGER.error(
                    "Clinic evidence prefetch failed (tool=%s, error_type=%s)",
                    name, type(exc).__name__,
                )
                return None
            call_id = f"evidence-{uuid4().hex}"
            calls.append({
                "name": name, "args": args, "id": call_id, "type": "tool_call",
            })
            results.append(ToolMessage(
                content=json.dumps(result, ensure_ascii=False, default=str),
                name=name, tool_call_id=call_id,
            ))
            return result

        directory_result = (
            await fetch(directory.name, {}) if directory is not None else None
        )
        doctors = _doctors_from_directory(directory_result)
        doctor_ids = _mentioned_doctors(question, doctors)
        if doctor_ids:
            for doctor_id in doctor_ids:
                if any(term in lower for term in ("queue", "ticket", "serving", "current number")):
                    await fetch("clinic_operations_get_queue_status", {"doctor_id": doctor_id})
                if any(term in lower for term in ("procedure", "surgery", "operation")):
                    await fetch("clinic_operations_get_procedure_status", {"doctor_id": doctor_id})
                if any(term in lower for term in ("schedule", "location", "where", "normally work")):
                    await fetch("clinic_operations_get_doctor_schedule", {"doctor_id": doctor_id})
                if any(term in lower for term in ("appointment", "slot", "availability")):
                    await fetch("clinic_appointments_list_available_appointments", {"doctor_id": doctor_id})
        if any(term in lower for term in policy_terms) or _asks_missed_number_policy(question):
            if _asks_missed_number_policy(question):
                await fetch(
                    "clinic_knowledge_read_knowledge_document",
                    {"document_id": "queue-and-wait-times"},
                )
            if any(term in lower for term in ("booked", "booking", "confirmed")):
                await fetch(
                    "clinic_knowledge_read_knowledge_document",
                    {"document_id": "appointments-and-check-in"},
                )
            if not _asks_missed_number_policy(question) or any(
                term in lower for term in (
                    "guarantee", "booked", "booking", "confirmed", "hours",
                    "parking", "privacy", "contact", "check-in", "late",
                )
            ):
                await fetch(
                    "clinic_knowledge_search_clinic_knowledge",
                    {"query": question, "max_results": 3},
                )
        if not calls:
            return {"messages": []}
        return {"messages": [AIMessage(content="", tool_calls=calls), *results]}

    return prefetch


def _build_chat_graph(model: ChatOpenAI, tools: list[BaseTool]):
    return _build_agent_graph(
        model, tools, SYSTEM_PROMPT + (EXTERNAL_GUIDANCE if tavily_enabled() else ""), checkpointer=chat_memory,
        agent_name="single", prefetch=_single_evidence_prefetch(tools),
    )


def _build_supervisor_graph(
    model: BaseChatModel,
    tools: list[BaseTool],
    *,
    specialist_models: dict[str, BaseChatModel] | None = None,
):
    """Build a LangGraph manager with three actual tool-using agent graphs."""
    by_name = {item.name: item for item in tools}
    groups = {
        "policy": [
            item for item in tools if item.name.startswith("clinic_knowledge_")
        ],
        "operations": [
            item for item in tools if item.name.startswith("clinic_operations_")
        ],
        "appointments": [
            item for item in tools
            if item.name == "clinic_operations_list_doctors"
            or (
                item.name.startswith("clinic_appointments_")
                and item.name != "clinic_appointments_book_appointment"
            )
        ],
    }
    required = {
        "clinic_knowledge_search_clinic_knowledge",
        "clinic_operations_list_doctors",
        "clinic_operations_get_queue_status",
        "clinic_appointments_list_available_appointments",
    }
    if not required.issubset(by_name) or any(not group for group in groups.values()):
        raise MCPConnectionError(
            "MCP connection error: supervisor specialists lack required tools."
        )

    specialist_models = specialist_models or {}
    specialists = {
        name: _build_agent_graph(
            specialist_models.get(name, model), group,
            SPECIALIST_PROMPTS[name], agent_name=name,
            parallel_tool_calls=None if name in specialist_models else True,
            provider="nvidia" if name in specialist_models else "openai",
            prefetch=_single_evidence_prefetch(group),
            answer_from_prefetched_tools=(
                ("clinic_appointments_list_available_appointments",)
                if name == "appointments" else
                ("clinic_knowledge_search_clinic_knowledge",
                 "clinic_knowledge_read_knowledge_document")
                if name == "policy" else ()
            ),
        )
        for name, group in groups.items()
    }
    # Connected stdio MCP sessions may reuse context. Keep specialist tool
    # invocations sequential even if a provider emits several manager calls.
    specialist_lock = asyncio.Lock()

    async def consult(name: str, question: str) -> str:
        started_at = time.perf_counter()
        if name == "policy" and _asks_missed_number_policy(question):
            # The public missed-number rule has a known document ID. Fetch it
            # directly so a specialist cannot guess a different policy ID.
            document_tool = by_name.get(
                "clinic_knowledge_read_knowledge_document"
            )
            document = None
            if document_tool is not None:
                try:
                    result = await document_tool.ainvoke(
                        {"document_id": "queue-and-wait-times"}
                    )
                    document = next(
                        (
                            item for item in _nested_evidence_objects(result)
                            if item.get("document_id") == "queue-and-wait-times"
                            and isinstance(item.get("content"), str)
                            and re.search(
                                r"(?im)^## Missed number\s*$",
                                item["content"],
                            )
                        ),
                        None,
                    )
                except Exception as exc:
                    LOGGER.error(
                        "Missed-number policy fetch failed (error_type=%s)",
                        type(exc).__name__,
                    )
            return json.dumps({
                "answer": (
                    "Verified missed-number policy is in the document evidence."
                    if document is not None else
                    "The missed-number policy could not be verified."
                ),
                "verified_sources": (
                    ["clinic_knowledge_read_knowledge_document", "queue-and-wait-times"]
                    if document is not None else []
                ),
                "verified_data": (
                    [{"tool": "clinic_knowledge_read_knowledge_document", "result": document}]
                    if document is not None else []
                ),
            }, ensure_ascii=False)
        async with specialist_lock:
            state = await specialists[name].ainvoke(
                {"messages": [HumanMessage(content=question)]},
                config={"recursion_limit": 12},
            )
        sources = _verified_sources_for_messages(state["messages"])
        latest_question_index = max(
            (index for index, item in enumerate(state["messages"])
             if isinstance(item, HumanMessage)),
            default=-1,
        )
        verified_data = [
            {"tool": item.name, "result": item.content}
            for item in state["messages"][latest_question_index + 1:]
            if isinstance(item, ToolMessage)
            and item.status != "error"
            and item.name in {
                "clinic_knowledge_read_knowledge_document",
                "clinic_operations_list_doctors",
                "clinic_operations_get_queue_status",
                "clinic_operations_get_procedure_status",
                "clinic_operations_get_doctor_schedule",
                "clinic_appointments_list_available_appointments",
            }
        ]
        message = state["messages"][-1]
        answer = message.content if isinstance(message.content, str) else ""
        if not sources:
            answer = "No verified clinic information was retrieved for this question."
        PERFORMANCE_LOGGER.info(
            "Clinic specialist completed (agent=%s, duration_ms=%d, source_count=%d)",
            name,
            round((time.perf_counter() - started_at) * 1000),
            len(sources),
        )
        return json.dumps(
            {
                "answer": answer,
                "verified_sources": sources,
                "verified_data": verified_data,
            },
            ensure_ascii=False,
        )

    @tool("consult_policy_agent")
    async def consult_policy_agent(question: str) -> str:
        """Consult the policy/RAG agent about documented clinic rules and services."""
        return await consult("policy", question)

    @tool("consult_operations_agent")
    async def consult_operations_agent(question: str) -> str:
        """Consult the operations agent about doctors, queues, and schedules."""
        return await consult("operations", question)

    @tool("consult_appointments_agent")
    async def consult_appointments_agent(question: str) -> str:
        """Consult the read-only appointments agent about available demo slots."""
        return await consult("appointments", question)

    consult_tools = {
        "policy": consult_policy_agent,
        "operations": consult_operations_agent,
        "appointments": consult_appointments_agent,
    }

    async def prefetch_specialists(state: MessagesState) -> dict[str, list[Any]]:
        question = _procedure_followup_question(state) or _latest_question(state)
        required = _required_specialists(question)
        if not required:
            return {"messages": []}
        calls = []
        results = []
        for name in required:
            call_id = f"route-{name}-{uuid4().hex}"
            tool_name = consult_tools[name].name
            calls.append({
                "name": tool_name, "args": {"question": question},
                "id": call_id, "type": "tool_call",
            })
            results.append(ToolMessage(
                content=await consult(name, question),
                name=tool_name, tool_call_id=call_id,
            ))
        return {"messages": [AIMessage(content="", tool_calls=calls), *results]}

    return _build_agent_graph(
        model,
        list(consult_tools.values()) + ([official_resource_search] if tavily_enabled() else []),
        SUPERVISOR_PROMPT + (EXTERNAL_GUIDANCE if tavily_enabled() else ""),
        checkpointer=supervisor_memory,
        parallel_tool_calls=False,
        agent_name="supervisor",
        prefetch=prefetch_specialists,
        answer_from_prefetched_tools=(
            "consult_operations_agent", "consult_appointments_agent",
            "consult_policy_agent",
        ),
        require_all_prefetched_tools=True,
        answer_ready_tools_for_question=_supervisor_answer_ready_tools,
    )


def _configured_agent_mode() -> str:
    mode = os.getenv("CLINIC_AGENT_MODE", "single").strip().lower()
    if mode not in {"single", "supervisor"}:
        raise LLMConfigurationError(
            "LLM configuration error: CLINIC_AGENT_MODE must be single or supervisor."
        )
    return mode


def _configured_provider_routing() -> str:
    routing = os.getenv("CLINIC_PROVIDER_ROUTING", "openai").strip().lower()
    if routing not in {"openai", "domain"}:
        raise LLMConfigurationError(
            "LLM configuration error: CLINIC_PROVIDER_ROUTING must be "
            "openai or domain."
        )
    if routing == "domain" and _configured_agent_mode() != "supervisor":
        raise LLMConfigurationError(
            "LLM configuration error: domain routing requires "
            "CLINIC_AGENT_MODE=supervisor."
        )
    return routing


def _build_runtime_graph(model: ChatOpenAI, tools: list[BaseTool]):
    if tavily_enabled():
        tools = [*tools, official_resource_search]
    if _configured_agent_mode() == "supervisor":
        if _configured_provider_routing() == "domain":
            return _build_supervisor_graph(
                model, tools,
                specialist_models={"policy": _create_nvidia_model()},
            )
        return _build_supervisor_graph(model, tools)
    _configured_provider_routing()
    return _build_chat_graph(model, tools)


_runtime_adapter: MCPAdapter | None = None
_runtime_graph: Any | None = None
_runtime_invocation_lock: asyncio.Lock | None = None


def chat_runtime_ready() -> bool:
    """Return whether the process has a persistent model, graph, and MCP clients."""
    return _runtime_adapter is not None and _runtime_graph is not None


async def initialize_chat_runtime() -> None:
    """Initialize one reusable agent graph and keep its MCP clients connected."""
    global _runtime_adapter, _runtime_graph, _runtime_invocation_lock

    if chat_runtime_ready():
        return

    started_at = time.perf_counter()
    model = _create_model()
    adapter = MCPAdapter(_load_mcp_config())

    try:
        await adapter.__aenter__()
        tools = await adapter.list_tools()
        graph = _build_runtime_graph(model, tools)
    except Exception:
        await adapter.__aexit__(*sys.exc_info())
        raise

    _runtime_adapter = adapter
    _runtime_graph = graph
    _runtime_invocation_lock = asyncio.Lock()
    PERFORMANCE_LOGGER.info(
        "Persistent clinic agent runtime initialized "
        "(agent_mode=%s, provider_routing=%s, tool_count=%d, duration_ms=%d)",
        _configured_agent_mode(),
        _configured_provider_routing(),
        len(tools),
        round((time.perf_counter() - started_at) * 1000),
    )


async def shutdown_chat_runtime() -> None:
    """Close persistent MCP clients during application shutdown."""
    global _runtime_adapter, _runtime_graph, _runtime_invocation_lock

    adapter = _runtime_adapter
    _runtime_adapter = None
    _runtime_graph = None
    _runtime_invocation_lock = None
    if adapter is not None:
        await adapter.__aexit__(None, None, None)
        PERFORMANCE_LOGGER.info("Persistent clinic agent runtime stopped")


async def create_chat_response(
    query: str, session_id: str, *, usage_sink: list[dict[str, Any]] | None = None
) -> str:
    """Send a query through LangGraph with session-scoped memory."""
    config = {"configurable": {"thread_id": session_id}}
    started_at = time.perf_counter()
    using_persistent_runtime = chat_runtime_ready()
    usage_token = _evaluation_usage_sink.set(usage_sink)
    search_budget_token = TURN_BUDGET.set({"used": 0})

    try:
        if using_persistent_runtime:
            chat_graph = _runtime_graph
            invocation_lock = _runtime_invocation_lock
        else:
            model = _create_model()
            tools = await _load_mcp_tools()
            chat_graph = _build_runtime_graph(model, tools)
            invocation_lock = None

        if chat_graph is None:
            raise RuntimeError("The clinic agent graph is unavailable.")

        input_state = {"messages": [HumanMessage(content=query)]}
        if invocation_lock is None:
            state = await chat_graph.ainvoke(input_state, config=config)
        else:
            # A connected stdio MCP client reuses its server sessions. Serialize
            # agent turns so concurrent requests cannot mix MCP session context.
            async with invocation_lock:
                state = await chat_graph.ainvoke(input_state, config=config)
    except (LLMConfigurationError, MCPConnectionError):
        raise
    except Exception as exc:
        LOGGER.error(
            "Agent model request failed (error_type=%s)",
            type(exc).__name__,
        )
        raise LLMRequestError(
            "The language model request failed. "
            "Check the model configuration and provider availability."
        ) from exc
    finally:
        _evaluation_usage_sink.reset(usage_token)
        TURN_BUDGET.reset(search_budget_token)

    message = state["messages"][-1]

    if not isinstance(message.content, str) or not message.content.strip():
        raise LLMRequestError("The language model returned an invalid response.")

    PERFORMANCE_LOGGER.info(
        "Clinic agent response completed (duration_ms=%d, persistent_runtime=%s)",
        round((time.perf_counter() - started_at) * 1000),
        using_persistent_runtime,
    )
    return append_external_sources(
        _answer_with_verified_sources(message.content, state["messages"]), state["messages"]
    )
