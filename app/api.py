import os
import logging
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.models import ChatRequest, ChatResponse, QueryText
from app.services.result_cards import BookingConfirmation, card_context, verify_selection, Slot
from app.services.chat import confirm_appointment_selection
from app.rate_limit import chat_rate_limiter, clinic_rate_limiter
from app.services.clinic_actions import ClinicLookup, doctor_directory, lookup
from app.services.booking_owner import browser_owner, booking_owner_context, check_booking_origin
from app.services.my_appointments import CancelReservation, my_appointments, cancel_appointment
from app.services.maps import maps_enabled, search_places
from app.services.tavily import tavily_enabled
from app.services.chat import (
    LLMConfigurationError,
    LLMRequestError,
    MCPConnectionError,
    create_chat_response,
    initialize_chat_runtime,
    shutdown_chat_runtime,
)


DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() == "true"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIRECTORY = PROJECT_ROOT / "static"
LOGGER = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Keep MCP subprocesses and the compiled agent graph warm per instance."""
    if _public_chat_enabled():
        try:
            await initialize_chat_runtime()
        except Exception as exc:
            # FAQ mode must remain available if model or MCP startup fails. A
            # later chat request uses the existing controlled 502/503 handling.
            LOGGER.error(
                "Persistent clinic agent startup failed (error_type=%s)",
                type(exc).__name__,
            )
    try:
        yield
    finally:
        await shutdown_chat_runtime()


app = FastAPI(
    title="Intelligent Clinic Assistant",
    version="2.0.0",
    lifespan=lifespan,
)


app.mount(
    "/static",
    StaticFiles(directory=STATIC_DIRECTORY),
    name="static"
)


DEMO_RESPONSES = {
    "specialties-and-locations": {
        "question": "Which specialties are shown in this sample clinic?",
        "answer": (
            "This synthetic outpatient clinic shows Family Medicine, General "
            "Surgery, Dermatology, and Otolaryngology (ENT). Doctor locations "
            "and available appointments can differ, so use the current schedule "
            "and appointment information before visiting. This list is not "
            "medical advice about which specialty to choose."
        ),
        "sources": [{"source": "specialties-and-locations"}],
    },
    "clinic-hours": {
        "question": "What are the clinic's opening hours?",
        "answer": (
            "The demo clinic is open Monday through Friday from 9:00 AM to "
            "12:00 PM and from 2:00 PM to 6:00 PM Pacific Time. It is normally "
            "closed on weekends and public holidays."
        ),
        "sources": [{"source": "clinic-hours"}]
    },
    "parking": {
        "question": "Does the clinic have visitor parking?",
        "answer": (
            "Yes. The demo clinic provides free, designated visitor parking "
            "at each location during appointments. Accessible spaces and a "
            "passenger drop-off area are available near the main entrance. "
            "Spaces are limited and first-come, first-served."
        ),
        "sources": [{"source": "getting-here-and-parking"}]
    },
    "check-in-and-late-arrival": {
        "question": "How early should I arrive, and what happens if I am late?",
        "answer": (
            "Patients should arrive 15 minutes before a scheduled appointment "
            "for check-in. Late arrivals may need to be rescheduled when the "
            "remaining visit time would not be sufficient for safe care."
        ),
        "sources": [{"source": "appointments-and-check-in"}]
    },
    "cancel-or-reschedule": {
        "question": "How do I cancel or reschedule an appointment?",
        "answer": (
            "Contact the clinic as early as possible to cancel or reschedule "
            "so the appointment time can be offered to another patient."
        ),
        "sources": [{"source": "appointments-and-check-in"}]
    },
    "appointment-requests": {
        "question": "How do online appointment requests work?",
        "answer": (
            "Appointment requests may be submitted online, but a request is "
            "not confirmed until the clinic sends a confirmation. The public "
            "website must not collect detailed medical records or government "
            "health-card data."
        ),
        "sources": [{"source": "appointments-and-check-in"}]
    },
    "wait-time-estimates": {
        "question": "Are clinic wait-time estimates guaranteed?",
        "answer": (
            "No. Wait-time figures are estimates, not guarantees. They can "
            "change because of urgent clinical needs, longer visits, schedule "
            "changes, or procedures requiring additional time."
        ),
        "sources": [{"source": "queue-and-wait-times"}]
    },
    "procedure-status-information": {
        "question": "What do the procedure status labels mean?",
        "answer": (
            "The public status board may show that a doctor is available, "
            "preparing, in a procedure, or delayed. An estimated completion "
            "time may be shown for a procedure in progress and can be updated "
            "by authorized clinic staff."
        ),
        "sources": [{"source": "procedure-status"}]
    },
    "public-information-privacy": {
        "question": "What information is shown on the public queue display?",
        "answer": (
            "The public display uses queue numbers. It must not show patient "
            "names, medical record numbers, diagnoses, or other identifying "
            "health information."
        ),
        "sources": [{"source": "queue-and-wait-times"}]
    },
    "urgent-care": {
        "question": "Can I use this website for urgent medical help?",
        "answer": (
            "No. This website is not an emergency service and must not be used "
            "for urgent medical advice. Anyone experiencing a medical emergency "
            "should call 911 or go to the nearest emergency department."
        ),
        "sources": [{"source": "urgent-care-and-emergencies"}]
    },
    "contact-information": {
        "question": "What is the clinic's phone number?",
        "answer": (
            "The sample clinic's main telephone number is (213) 555-0147. "
            "Staff answer Monday through Friday from 9:00 AM to 12:00 PM and "
            "from 2:00 PM to 6:00 PM Pacific Time. This synthetic telephone "
            "number is for the portfolio prototype and is not an emergency line."
        ),
        "sources": [{"source": "contact-information"}]
    }
}


class QueryRequest(BaseModel):
    question: QueryText


class DemoRequest(BaseModel):
    demo_id: str


class QueryResponse(BaseModel):
    answer: str
    sources: list[dict]


@app.get("/")
def home():
    return FileResponse(
        STATIC_DIRECTORY / "index.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "demo_mode": DEMO_MODE,
        "agent": "enabled",
        "mcp_servers": 3,
        "public_chat_enabled": _public_chat_enabled(),
    }


def _public_chat_enabled() -> bool:
    return os.getenv("PUBLIC_CHAT_ENABLED", "true").strip().lower() == "true"


def _positive_int_environment(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", maxsplit=1)[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _check_chat_access(http_request: Request) -> None:
    """Apply the same public switch and IP quota to both chat endpoints."""
    if not _public_chat_enabled():
        raise HTTPException(
            status_code=403,
            detail="Live agent requests are disabled for this public demo.",
        )

    rate_limit = chat_rate_limiter.check(
        _client_ip(http_request),
        per_minute=_positive_int_environment("CHAT_RATE_LIMIT_PER_MINUTE", 3),
        per_day=_positive_int_environment("CHAT_RATE_LIMIT_PER_DAY", 10),
    )
    if not rate_limit.allowed:
        if rate_limit.reason == "day":
            detail = "The daily Live Agent request limit has been reached."
        else:
            detail = "The per-minute Live Agent request limit has been reached."
        raise HTTPException(
            status_code=429,
            detail=detail,
            headers={
                "Retry-After": str(rate_limit.retry_after),
                "X-Rate-Limit-Reason": rate_limit.reason or "unknown",
            },
        )


class MapsSearchRequest(BaseModel):
    query: QueryText


@app.get("/maps/status")
def maps_status():
    return {"configured": maps_enabled() and _public_chat_enabled()}


@app.get("/capabilities")
def capabilities():
    """Expose configured UI features, never credentials or connectivity claims."""
    return JSONResponse(
        {"official_resources": tavily_enabled() and _public_chat_enabled()},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/maps/search")
async def maps_search(payload: MapsSearchRequest, request: Request):
    if not maps_enabled():
        raise HTTPException(503, "Location lookup is not connected yet.")
    _check_chat_access(request)
    try:
        result = await search_places(payload.query)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        LOGGER.warning("Maps search unavailable (error_type=%s)", type(exc).__name__)
        raise HTTPException(502, "Google Maps lookup is temporarily unavailable. Please try again later.") from None


def _chat_error(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, LLMConfigurationError):
        LOGGER.error("Agent configuration failed (error_type=%s)", type(exc).__name__)
        return 503, "The clinic agent is temporarily unavailable."
    if isinstance(exc, MCPConnectionError):
        LOGGER.error("Clinic tool connection failed (error_type=%s)", type(exc).__name__)
        return 502, "Clinic tools are temporarily unavailable."
    if isinstance(exc, LLMRequestError):
        LOGGER.error("Model request failed (error_type=%s)", type(exc).__name__)
        return 502, "The language model service is temporarily unavailable."
    LOGGER.error("Unhandled chat error (error_type=%s)", type(exc).__name__)
    return 500, "An unexpected server error occurred."


async def _chat_result(request: ChatRequest, owner_hash: str | None = None) -> ChatResponse:
    cards = []
    token = card_context.set((request.session_id, cards))
    owner_token = booking_owner_context.set(owner_hash)
    try:
        response = await create_chat_response(request.query, request.session_id)
        return ChatResponse(response=response, cards=cards)
    finally:
        card_context.reset(token)
        booking_owner_context.reset(owner_token)


def _check_clinic_access(request: Request) -> None:
    if not _public_chat_enabled():
        raise HTTPException(status_code=403, detail="Clinic actions are currently unavailable. You can still browse all clinic FAQs.")
    decision = clinic_rate_limiter.check(
        _client_ip(request),
        per_minute=_positive_int_environment("CLINIC_RATE_LIMIT_PER_MINUTE", 30),
        per_day=_positive_int_environment("CLINIC_RATE_LIMIT_PER_DAY", 200),
    )
    if not decision.allowed:
        raise HTTPException(status_code=429, detail="The clinic action limit has been reached. Please try again later.",
            headers={"Retry-After": str(decision.retry_after), "X-Rate-Limit-Reason": decision.reason or "unknown"})


@app.get("/clinic/doctors")
async def clinic_doctors(request: Request):
    _check_clinic_access(request)
    try:
        return await doctor_directory()
    except Exception as exc:
        LOGGER.warning("Doctor directory unavailable (error_type=%s)", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Doctor information is temporarily unavailable. Please try again.") from exc


@app.post("/clinic/lookup", response_model=ChatResponse)
async def clinic_lookup(payload: ClinicLookup, request: Request):
    _check_clinic_access(request)
    try:
        return await lookup(payload)
    except ValueError as exc:
        # A missing doctor is a user selection error; upstream content stays private.
        if payload.action != "appointments" and not payload.doctor_id:
            raise HTTPException(status_code=400, detail="Select a doctor first.") from exc
        raise HTTPException(status_code=502, detail="Clinic information is temporarily unavailable. Please try again.") from exc
    except Exception as exc:
        LOGGER.warning("Clinic lookup failed (error_type=%s)", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Clinic information is temporarily unavailable. Please try again.") from exc


@app.post("/appointments/confirm", response_model=ChatResponse)
async def confirm_appointment(request: BookingConfirmation, http_request: Request, http_response: Response):
    _check_clinic_access(http_request)
    check_booking_origin(http_request)
    http_response.headers["Cache-Control"] = "no-store"
    try:
        slot_id = verify_selection(request.selection_token, request.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        owner_hash = browser_owner(http_request, http_response)
        reservation = await confirm_appointment_selection(slot_id, request.session_id, owner_hash)
        slot = Slot.model_validate(reservation["slot"]).model_dump()
    except MCPConnectionError as exc:
        raise HTTPException(status_code=502, detail="Appointment tools are temporarily unavailable.") from exc
    except Exception as exc:
        LOGGER.warning("Appointment confirmation failed (error_type=%s)", type(exc).__name__)
        raise HTTPException(status_code=409, detail="This slot could not be reserved. Refresh available times before trying again.") from exc
    return ChatResponse(response="Your demo appointment is reserved. No patient information was collected.",
        cards=[{"kind": "reservation", "slot": slot,
                "booking_reference": reservation["booking_reference"],
                "source": "clinic_appointments_book_appointment"}])


@app.get("/appointments/mine", response_model=ChatResponse)
async def owned_appointments(request: Request, response: Response):
    _check_clinic_access(request)
    owner_hash = browser_owner(request, response)
    response.headers["Cache-Control"] = "no-store"
    try:
        cards = await my_appointments(owner_hash)
    except Exception as exc:
        LOGGER.warning("Reservation lookup failed (error_type=%s)", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Your reservations are temporarily unavailable. Please try again.") from exc
    return ChatResponse(response="Your demo reservations saved in this browser." if cards else "No reservations saved in this browser yet. Book an appointment to get started.", cards=cards)


@app.post("/appointments/cancel", response_model=ChatResponse)
async def cancel_owned_reservation(payload: CancelReservation, request: Request, response: Response):
    _check_clinic_access(request)
    check_booking_origin(request)
    owner_hash = browser_owner(request, response)
    response.headers["Cache-Control"] = "no-store"
    try:
        card = await cancel_appointment(payload.booking_reference, owner_hash)
    except Exception as exc:
        LOGGER.warning("Reservation cancellation failed (error_type=%s)", type(exc).__name__)
        raise HTTPException(status_code=409, detail="This reservation could not be cancelled. Refresh My appointments before trying again.") from exc
    return ChatResponse(response="Your demo reservation is cancelled and the time is available again.", cards=[card])


@app.post("/chat", response_model=ChatResponse, response_model_exclude_defaults=True)
async def chat(request: ChatRequest, http_request: Request, http_response: Response) -> ChatResponse:
    """Run one session-scoped LangGraph agent turn with MCP tools."""
    _check_chat_access(http_request)
    try:
        result = await _chat_result(request, browser_owner(http_request, http_response))
    except Exception as exc:
        status, detail = _chat_error(exc)
        raise HTTPException(status_code=status, detail=detail) from exc

    return result


def _sse_event(name: str, data: dict[str, object]) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest, http_request: Request, http_response: Response) -> StreamingResponse:
    """Stream progress and a final answer; model tokens are not streamed."""
    _check_chat_access(http_request)
    owner_hash = browser_owner(http_request, http_response)

    async def events() -> AsyncIterator[str]:
        yield _sse_event("progress", {"message": "Lumi is checking clinic sources..."})
        try:
            result = await _chat_result(request, owner_hash)
        except Exception as exc:
            status, detail = _chat_error(exc)
            yield _sse_event("error", {"message": detail, "status": str(status)})
            return
        yield _sse_event("answer", result.model_dump(exclude_defaults=True))
        yield _sse_event("done", {})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 **({"Set-Cookie": http_response.headers["set-cookie"]} if "set-cookie" in http_response.headers else {})},
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    """Return a controlled message without exposing internal exception details."""
    LOGGER.error(
        "Unhandled API error (path=%s, error_type=%s)",
        request.url.path,
        type(error).__name__,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected server error occurred."},
    )


@app.post("/demo/query", response_model=QueryResponse)
def demo_query(request: DemoRequest):
    result = DEMO_RESPONSES.get(request.demo_id)

    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Unknown demo scenario."
        )

    return {
        "answer": result["answer"],
        "sources": result["sources"]
    }


@app.post("/query", response_model=QueryResponse)
def query(request: QueryRequest):
    if DEMO_MODE:
        raise HTTPException(
            status_code=403,
            detail=(
                "Interactive LLM queries are disabled "
                "in public demo mode."
            )
        )

    from app.rag.rag import (
        filter_relevant_results,
        generate_answer,
    )
    from app.rag.retriever import search

    retrieved_results = search(
        request.question,
        top_k=2,
        min_similarity=0.50
    )

    relevant_results = filter_relevant_results(
        request.question,
        retrieved_results
    )

    answer = generate_answer(
        request.question,
        relevant_results
    )

    sources = []

    for result in relevant_results:
        metadata = result["metadata"]

        sources.append({
            "source": metadata["source"],
            "chunk_id": metadata["chunk_id"],
            "score": round(result["score"], 4)
        })

    return {
        "answer": answer,
        "sources": sources
    }
