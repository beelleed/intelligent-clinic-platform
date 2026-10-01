import os
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.models import ChatRequest, ChatResponse, QueryText
from app.rate_limit import chat_rate_limiter
from app.services.chat import (
    LLMConfigurationError,
    LLMRequestError,
    MCPConnectionError,
    create_chat_response,
)


DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() == "true"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIRECTORY = PROJECT_ROOT / "static"
LOGGER = logging.getLogger(__name__)


app = FastAPI(
    title="Intelligent Clinic Assistant",
    version="2.0.0"
)


app.mount(
    "/static",
    StaticFiles(directory=STATIC_DIRECTORY),
    name="static"
)


DEMO_RESPONSES = {
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


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, http_request: Request) -> ChatResponse:
    """Run one session-scoped LangGraph agent turn with MCP tools."""
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

    try:
        response = await create_chat_response(request.query, request.session_id)
    except LLMConfigurationError as exc:
        LOGGER.error("Agent configuration failed (error_type=%s)", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="The clinic agent is temporarily unavailable.",
        ) from exc
    except MCPConnectionError as exc:
        LOGGER.error("Clinic tool connection failed (error_type=%s)", type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail="Clinic tools are temporarily unavailable.",
        ) from exc
    except LLMRequestError as exc:
        LOGGER.error("Model request failed (error_type=%s)", type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail="The language model service is temporarily unavailable.",
        ) from exc

    return ChatResponse(response=response)


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
