from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from fastapi.responses import FileResponse

from app.rag.rag import (
    filter_relevant_results,
    generate_answer,
)
from app.rag.retriever import search
from app.rag.vector_store import initialize_vector_store


app = FastAPI(
    title="AI-Powered Clinic Knowledge Assistant",
    version="1.0.0"
)

@app.on_event("startup")
def startup_event():
    initialize_vector_store()

app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static"
)


class QueryRequest(BaseModel):
    question: str


class QueryResponse(BaseModel):
    answer: str
    sources: list[dict]

@app.get("/")
def home():
    return FileResponse("static/index.html")

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/query", response_model=QueryResponse)
def query(request: QueryRequest):
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