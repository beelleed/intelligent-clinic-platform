"""Booking actions must come from current MCP evidence and explicit confirmation."""

import json
import sys
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.api import app
from app.rate_limit import chat_rate_limiter, clinic_rate_limiter
from app.services.result_cards import appointment_cards, selection_token, verify_selection


SLOT = {"slot_id": 3, "doctor_id": "dr-rivera", "doctor_name": "Dr. Sofia Rivera",
        "appointment_date": "2099-11-19", "start_time": "09:00", "end_time": "09:30",
        "timezone": "America/Los_Angeles"}
TOOL = "clinic_appointments_list_available_appointments"


def evidence(data, name=TOOL):
    return ToolMessage(content=json.dumps(data), name=name, tool_call_id="test-call")


def test_cards_use_current_tool_evidence_and_never_model_prose():
    old = [HumanMessage(content="Earlier availability"), evidence({"slots": [SLOT]})]
    assert appointment_cards(old + [HumanMessage(content="Hello"), AIMessage(content=json.dumps({"slots": [SLOT]}))], "s") == []
    cards = appointment_cards([HumanMessage(content="Availability"), evidence({"slots": [SLOT, SLOT]})], "s")
    assert len(cards) == 1
    assert cards[0]["slot"] == SLOT
    assert verify_selection(cards[0]["selection_token"], "s") == 3


def test_supervisor_cards_use_only_verified_specialist_evidence():
    report = {"verified_data": [{"tool": TOOL, "result": {"slots": [SLOT]}}]}
    cards = appointment_cards([HumanMessage(content="Availability"), evidence(report, "consult_appointments")], "s")
    assert cards[0]["slot"] == SLOT
    assert appointment_cards([HumanMessage(content="Availability"), evidence({"slots": [SLOT]}, "unrelated_tool")], "s") == []


def test_failed_tools_cannot_offer_a_slot():
    failed = evidence({"slots": [SLOT]})
    failed.status = "error"
    assert appointment_cards([HumanMessage(content="Availability"), failed], "s") == []


def test_reservation_supersedes_earlier_available_cards():
    messages = [HumanMessage(content="Book"), evidence({"slots": [SLOT]}),
                evidence({"slot": SLOT, "booking_reference": "APT-TEST"}, "clinic_appointments_book_appointment")]
    cards = appointment_cards(messages, "s")
    assert len(cards) == 1 and cards[0]["kind"] == "reservation"
    assert "selection_token" not in cards[0]


def test_selection_rejects_tampering_other_sessions_and_expiry(monkeypatch):
    monkeypatch.setattr("app.services.result_cards.time.time", lambda: 1000)
    token = selection_token(3, "owner")
    for invalid, session in [(token, "other"), (token + "x", "owner"), ("bad", "owner")]:
        with pytest.raises(ValueError):
            verify_selection(invalid, session)
    monkeypatch.setattr("app.services.result_cards.time.time", lambda: 1900)
    with pytest.raises(ValueError):
        verify_selection(token, "owner")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PUBLIC_CHAT_ENABLED", "true")
    monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MINUTE", "100")
    chat_rate_limiter.reset()
    clinic_rate_limiter.reset()
    yield TestClient(app)
    chat_rate_limiter.reset()
    clinic_rate_limiter.reset()


def test_confirm_rejects_invalid_token_without_booking(client, monkeypatch):
    book = AsyncMock()
    monkeypatch.setattr("app.api.confirm_appointment_selection", book)
    response = client.post("/appointments/confirm", json={"session_id": "s", "selection_token": "tampered"})
    assert response.status_code == 400
    book.assert_not_awaited()


def test_confirm_uses_exact_slot_without_a_model_turn(client, monkeypatch):
    book = AsyncMock(return_value={"slot": SLOT, "booking_reference": "APT-TEST"})
    model = AsyncMock()
    monkeypatch.setattr("app.api.confirm_appointment_selection", book)
    monkeypatch.setattr("app.api.create_chat_response", model)
    response = client.post("/appointments/confirm", json={"session_id": "s", "selection_token": selection_token(3, "s")})
    assert response.status_code == 200
    assert response.json()["cards"][0]["booking_reference"] == "APT-TEST"
    book.assert_awaited_once()
    assert book.await_args.args[:2] == (3, "s")
    assert len(book.await_args.args[2]) == 64
    model.assert_not_awaited()


def test_conflict_is_controlled_and_public_switch_applies(client, monkeypatch):
    book = AsyncMock(side_effect=ValueError("private upstream detail"))
    monkeypatch.setattr("app.api.confirm_appointment_selection", book)
    request = {"session_id": "s", "selection_token": selection_token(3, "s")}
    response = client.post("/appointments/confirm", json=request)
    assert response.status_code == 409 and "private upstream" not in response.text
    monkeypatch.setenv("PUBLIC_CHAT_ENABLED", "false")
    assert client.post("/appointments/confirm", json=request).status_code == 403


@pytest.mark.anyio
@pytest.mark.parametrize("memory_error", [False, True])
async def test_confirmed_booking_updates_memory_without_losing_success_on_memory_error(monkeypatch, memory_error):
    from app.services.chat import confirm_appointment_selection

    reservation = {"slot": SLOT, "booking_reference": "APT-MEMORY"}
    tool = SimpleNamespace(name="clinic_appointments_book_appointment", ainvoke=AsyncMock(return_value=reservation))
    graph = SimpleNamespace(aupdate_state=AsyncMock(side_effect=RuntimeError("memory") if memory_error else None))
    monkeypatch.setattr("app.services.chat._runtime_adapter", SimpleNamespace(list_tools=AsyncMock(return_value=[tool])))
    monkeypatch.setattr("app.services.chat._runtime_invocation_lock", asyncio.Lock())
    monkeypatch.setattr("app.services.chat._runtime_graph", graph)
    assert await confirm_appointment_selection(3, "owner") == reservation
    config, state = graph.aupdate_state.await_args.args
    assert config["configurable"]["thread_id"] == "owner"
    assert any(isinstance(msg, ToolMessage) and json.loads(msg.content) == reservation for msg in state["messages"])


@pytest.mark.anyio
async def test_confirmation_uses_real_mcp_and_rejects_double_booking(monkeypatch, tmp_path):
    from mcp_servers.clinic_appointments.database import connect
    from app.services.chat import confirm_appointment_selection

    monkeypatch.setenv("CLINIC_APPOINTMENTS_DB_PATH", str(tmp_path / "appointments.db"))
    config = tmp_path / "mcp.json"
    servers = json.loads(Path("mcp_config.json").read_text())["mcpServers"]
    for server in servers.values():
        server["command"] = sys.executable
        server["env"] = {"CLINIC_APPOINTMENTS_DB_PATH": str(tmp_path / "appointments.db"),
                         "CLINIC_DB_PATH": str(tmp_path / "operations.db"),
                         "CLINIC_KNOWLEDGE_SEARCH_MODE": "lexical"}
    config.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    monkeypatch.setenv("MCP_SERVERS_CONFIG", str(config))
    connection = connect()
    from mcp_servers.clinic_time import clinic_today
    slot_id = connection.execute("SELECT id FROM appointment_slots WHERE appointment_date > ? ORDER BY id LIMIT 1", (clinic_today().isoformat(),)).fetchone()[0]
    connection.close()
    result = await confirm_appointment_selection(slot_id)
    assert result["slot"]["slot_id"] == slot_id
    assert result["booking_reference"].startswith("APT-")
    with pytest.raises(ValueError, match="could not be reserved"):
        await confirm_appointment_selection(slot_id)


@pytest.fixture
def anyio_backend():
    return "asyncio"
