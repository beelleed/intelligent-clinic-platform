"""Direct clinic actions must work without the model or its request budget."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from app.api import app
from app.rate_limit import chat_rate_limiter, clinic_rate_limiter
from app.services.result_cards import verify_selection


DIRECTORY = {"doctors": [{"id": "dr-rivera", "name": "Dr. Sofia Rivera", "specialty": "Dermatology"}]}
SLOT = {"slot_id": 5, "doctor_id": "dr-rivera", "doctor_name": "Dr. Sofia Rivera",
        "appointment_date": "2099-11-19", "start_time": "09:00", "end_time": "09:30", "timezone": "America/Los_Angeles"}
SCHEDULE = {"doctor_id": "dr-rivera", "doctor_name": "Dr. Sofia Rivera", "schedule": [
    {"weekday": "Thursday", "start_time": "09:00", "end_time": "12:00", "location": "Suite A"}]}
QUEUE = {"doctor_id": "dr-rivera", "doctor_name": "Dr. Sofia Rivera", "current_number": 12,
         "last_issued_number": 24, "updated_at": "2099-11-19T09:00:00-08:00", "timezone": "America/Los_Angeles"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PUBLIC_CHAT_ENABLED", "true")
    monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MINUTE", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    chat_rate_limiter.reset()
    clinic_rate_limiter.reset()
    yield TestClient(app)
    chat_rate_limiter.reset()
    clinic_rate_limiter.reset()


@pytest.mark.parametrize("action,kind,key,data", [
    ("appointments", "appointment_slot", "slots", {"slots": [SLOT]}),
    ("schedule", "schedule", "schedule", SCHEDULE),
    ("queue", "queue", "current_number", QUEUE),
])
def test_direct_action_returns_verified_data_without_a_model(client, monkeypatch, action, kind, key, data):
    reader = AsyncMock(return_value=[data])
    model = Mock(side_effect=AssertionError("Direct actions must not create a model"))
    monkeypatch.setattr("app.services.clinic_actions.read_clinic_tool", reader)
    monkeypatch.setattr("app.services.chat._create_model", model)
    result = client.post("/clinic/lookup", json={"action": action, "session_id": "owner", "doctor_id": "dr-rivera"})
    assert result.status_code == 200
    card = result.json()["cards"][0]
    assert card["kind"] == kind
    if action == "appointments":
        assert card["slot"] == SLOT
        assert verify_selection(card["selection_token"], "owner") == 5
    else:
        assert card["data"][key] == data[key]
    model.assert_not_called()


def test_clinic_navigation_does_not_exhaust_ai_quota(client, monkeypatch):
    monkeypatch.setattr("app.services.clinic_actions.read_clinic_tool", AsyncMock(return_value=[DIRECTORY]))
    for _ in range(4):
        assert client.get("/clinic/doctors").json() == DIRECTORY
    model = AsyncMock(return_value="Hello")
    monkeypatch.setattr("app.api.create_chat_response", model)
    assert client.post("/chat", json={"query": "Hello", "session_id": "s"}).status_code == 200
    assert client.post("/chat", json={"query": "Again", "session_id": "s"}).status_code == 429


def test_direct_actions_have_their_own_limit_and_public_switch(client, monkeypatch):
    reader = AsyncMock(return_value=[DIRECTORY])
    monkeypatch.setattr("app.services.clinic_actions.read_clinic_tool", reader)
    monkeypatch.setenv("CLINIC_RATE_LIMIT_PER_MINUTE", "1")
    assert client.get("/clinic/doctors").status_code == 200
    assert client.get("/clinic/doctors").status_code == 429
    reader.assert_awaited_once()
    monkeypatch.setenv("PUBLIC_CHAT_ENABLED", "false")
    assert client.get("/clinic/doctors").status_code == 403


def test_empty_availability_has_no_booking_action(client, monkeypatch):
    monkeypatch.setattr("app.services.clinic_actions.read_clinic_tool", AsyncMock(return_value=[{"slots": []}]))
    result = client.post("/clinic/lookup", json={"action": "appointments", "session_id": "s"})
    assert result.status_code == 200 and result.json()["cards"] == []
    assert "No available times" in result.json()["response"]


def test_date_selection_shows_only_that_days_times_and_returns_actual_available_dates(client, monkeypatch):
    later = {**SLOT, "slot_id": 6, "appointment_date": "2099-11-24", "start_time": "13:00", "end_time": "13:30"}
    monkeypatch.setattr("app.services.clinic_actions.read_clinic_tool", AsyncMock(return_value=[{"slots": [SLOT, later]}]))
    payload = {"action": "appointments", "session_id": "s", "doctor_id": "dr-rivera"}
    first = client.post("/clinic/lookup", json=payload).json()
    assert first["available_dates"] == ["2099-11-19", "2099-11-24"]
    assert first["selected_date"] == "2099-11-19"
    assert [card["slot"]["slot_id"] for card in first["cards"]] == [5]
    selected = client.post("/clinic/lookup", json={**payload, "appointment_date": "2099-11-24"}).json()
    assert selected["selected_date"] == "2099-11-24"
    assert [card["slot"]["slot_id"] for card in selected["cards"]] == [6]
    empty = client.post("/clinic/lookup", json={**payload, "appointment_date": "2099-11-25"}).json()
    assert empty["cards"] == []
    assert empty["available_dates"] == ["2099-11-19", "2099-11-24"]


def test_bad_input_and_upstream_errors_are_controlled(client, monkeypatch):
    reader = AsyncMock(side_effect=RuntimeError("private credential detail"))
    monkeypatch.setattr("app.services.clinic_actions.read_clinic_tool", reader)
    assert client.post("/clinic/lookup", json={"action": "book_anything", "session_id": "s"}).status_code == 422
    assert client.post("/clinic/lookup", json={"action": "appointments", "session_id": "s", "appointment_date": "not-a-date"}).status_code == 422
    assert client.post("/clinic/lookup", json={"action": "schedule", "session_id": "s"}).status_code == 400
    reader.assert_not_awaited()
    result = client.post("/clinic/lookup", json={"action": "queue", "session_id": "s", "doctor_id": "dr-rivera"})
    assert result.status_code == 502 and "private credential" not in result.text


def test_real_mcp_supports_selection_and_confirmation_without_model(client, monkeypatch, tmp_path):
    config = json.loads(Path("mcp_config.json").read_text())
    for server in config["mcpServers"].values():
        server["command"] = sys.executable
        server["env"] = {"CLINIC_DB_PATH": str(tmp_path / "operations.db"),
                         "CLINIC_APPOINTMENTS_DB_PATH": str(tmp_path / "appointments.db"),
                         "CLINIC_KNOWLEDGE_SEARCH_MODE": "lexical"}
    config_path = tmp_path / "mcp.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("MCP_SERVERS_CONFIG", str(config_path))
    model = Mock(side_effect=AssertionError("No LLM should run"))
    monkeypatch.setattr("app.services.chat._create_model", model)
    directory = client.get("/clinic/doctors")
    assert directory.status_code == 200 and len(directory.json()["doctors"]) == 4
    result = client.post("/clinic/lookup", json={"action": "appointments", "session_id": "s", "doctor_id": "dr-rivera"})
    assert result.status_code == 200
    card = result.json()["cards"][0]
    assert card["slot"]["doctor_id"] == "dr-rivera"
    request = {"session_id": "s", "selection_token": card["selection_token"]}
    booked = client.post("/appointments/confirm", json=request)
    assert booked.status_code == 200
    assert booked.json()["cards"][0]["slot"] == card["slot"]
    assert client.post("/appointments/confirm", json=request).status_code == 409
    model.assert_not_called()
