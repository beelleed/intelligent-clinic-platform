import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() == "true"


app = FastAPI(
    title="AI-Powered Clinic Knowledge Assistant",
    version="1.0.0"
)


app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static"
)


DEMO_RESPONSES = {
    "clinic-hours": {
        "question": "What are the clinic's opening hours?",
        "answer": (
            "The clinic is open Monday through Friday "
            "from 9:00 AM to 12:00 PM and from 2:00 PM to 6:00 PM."
        ),
        "sources": [
            {
                "source": "clinic_faq.txt",
                "chunk_id": 2,
                "score": 0.8024
            }
        ]
    },
    "surgery-delay": {
        "question": (
            "What should staff do if a procedure takes "
            "longer than expected?"
        ),
        "answer": (
            "If a procedure takes longer than expected, "
            "staff can extend the estimated completion time "
            "by 15, 30, or 60 minutes."
        ),
        "sources": [
            {
                "source": "clinic_faq.txt",
                "chunk_id": 9,
                "score": 0.6793
            }
        ]
    },
    "waiting-status": {
        "question": "What number does the clinic website display?",
        "answer": "The number currently being seen.",
        "sources": [
            {
                "source": "clinic_faq.txt",
                "chunk_id": 12,
                "score": 0.8341
            }
        ]
    },
    "unsupported": {
        "question": "What is the clinic's phone number?",
        "answer": (
            "I don't have enough information "
            "in the provided clinic documents."
        ),
        "sources": []
    }
}


class QueryRequest(BaseModel):
    question: str


class DemoRequest(BaseModel):
    demo_id: str


class QueryResponse(BaseModel):
    answer: str
    sources: list[dict]


@app.get("/")
def home():
    return FileResponse("static/index.html")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "demo_mode": DEMO_MODE
    }


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