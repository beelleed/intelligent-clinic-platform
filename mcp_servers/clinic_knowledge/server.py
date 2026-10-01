"""MCP tools for searching public clinic policy documents."""

from __future__ import annotations

import os
import re
import hashlib
import json
from pathlib import Path

import faiss
import numpy as np
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from app.rag.embeddings import create_embeddings


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KNOWLEDGE_DIRECTORY = PROJECT_ROOT / "clinic_knowledge"
DEFAULT_INDEX_DIRECTORY = PROJECT_ROOT / "data"
WORD_PATTERN = re.compile(r"[a-z0-9]+")

mcp = MCPServer(
    "Clinic Knowledge",
    instructions=(
        "Use these tools to answer questions about public clinic policies. "
        "Cite the returned document_id and do not infer private medical advice."
    ),
)


class DocumentSummary(BaseModel):
    document_id: str
    title: str


class DocumentList(BaseModel):
    documents: list[DocumentSummary]


class SearchMatch(BaseModel):
    document_id: str
    title: str
    score: float
    snippet: str


class SearchResults(BaseModel):
    query: str
    matches: list[SearchMatch]


class KnowledgeDocument(BaseModel):
    document_id: str
    title: str
    content: str


def _knowledge_directory() -> Path:
    configured_path = os.getenv("CLINIC_KNOWLEDGE_DIR")
    if configured_path:
        path = Path(configured_path).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()
    return DEFAULT_KNOWLEDGE_DIRECTORY


def _document_paths() -> dict[str, Path]:
    directory = _knowledge_directory()
    if not directory.is_dir():
        raise ToolError("The clinic knowledge directory is unavailable.")
    return {path.stem: path for path in sorted(directory.glob("*.md"))}


def _read_document(document_id: str) -> tuple[str, str]:
    path = _document_paths().get(document_id)
    if path is None:
        raise ToolError(
            f"Unknown document_id '{document_id}'. "
            "Call list_knowledge_documents to see valid IDs."
        )

    content = path.read_text(encoding="utf-8").strip()
    first_line = content.splitlines()[0] if content else ""
    title = first_line.removeprefix("# ").strip() or document_id
    return title, content


def _paragraphs(content: str) -> list[str]:
    return [
        " ".join(paragraph.split())
        for paragraph in re.split(r"\n\s*\n", content)
        if paragraph.strip() and not paragraph.lstrip().startswith("#")
    ]


def _knowledge_records() -> list[dict[str, str]]:
    records = []
    for document_id in _document_paths():
        title, content = _read_document(document_id)
        for paragraph in _paragraphs(content):
            records.append(
                {
                    "document_id": document_id,
                    "title": title,
                    "snippet": paragraph,
                }
            )
    return records


def _index_directory() -> Path:
    configured_path = os.getenv("CLINIC_KNOWLEDGE_INDEX_DIR", "").strip()
    if not configured_path:
        return DEFAULT_INDEX_DIRECTORY
    path = Path(configured_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _records_fingerprint(records: list[dict[str, str]]) -> str:
    payload = {
        "embedding_model": os.getenv(
            "OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"
        ),
        "records": records,
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _load_or_build_semantic_index(
    records: list[dict[str, str]],
) -> faiss.Index:
    index_directory = _index_directory()
    index_directory.mkdir(parents=True, exist_ok=True)
    index_path = index_directory / "clinic_knowledge.index"
    metadata_path = index_directory / "clinic_knowledge_index.json"
    fingerprint = _records_fingerprint(records)

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("fingerprint") == fingerprint:
            return faiss.read_index(str(index_path))
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    embeddings = np.asarray(
        create_embeddings([record["snippet"] for record in records]),
        dtype="float32",
    )
    if embeddings.ndim != 2 or not len(embeddings):
        raise ToolError("Clinic knowledge embeddings could not be created.")

    faiss.normalize_L2(embeddings)
    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)

    temporary_index = index_path.with_suffix(f".{os.getpid()}.tmp")
    temporary_metadata = metadata_path.with_suffix(f".{os.getpid()}.tmp")
    faiss.write_index(index, str(temporary_index))
    temporary_metadata.write_text(
        json.dumps({"fingerprint": fingerprint}, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary_index, index_path)
    os.replace(temporary_metadata, metadata_path)
    return index


def _semantic_matches(query: str, max_results: int) -> list[SearchMatch]:
    records = _knowledge_records()
    if not records:
        return []

    index = _load_or_build_semantic_index(records)
    query_vector = np.asarray(create_embeddings([query]), dtype="float32")
    faiss.normalize_L2(query_vector)
    scores, indices = index.search(query_vector, min(max_results, len(records)))

    matches = []
    for score, record_index in zip(scores[0], indices[0]):
        if record_index < 0:
            continue
        record = records[int(record_index)]
        matches.append(
            SearchMatch(
                **record,
                score=round(float(score), 4),
            )
        )
    return matches


def _lexical_matches(query: str, max_results: int) -> list[SearchMatch]:
    query_terms = set(WORD_PATTERN.findall(query.casefold()))
    ranked_matches = []
    for record in _knowledge_records():
        paragraph = record["snippet"]
        paragraph_terms = set(WORD_PATTERN.findall(paragraph.casefold()))
        score = len(query_terms & paragraph_terms)
        if query.casefold() in paragraph.casefold():
            score += 2
        if score:
            ranked_matches.append(SearchMatch(**record, score=float(score)))

    ranked_matches.sort(
        key=lambda match: (-match.score, match.document_id, match.snippet)
    )
    return ranked_matches[:max_results]


@mcp.tool()
def list_knowledge_documents() -> DocumentList:
    """List public clinic policy documents that can be searched or read."""
    documents = []
    for document_id in _document_paths():
        title, _ = _read_document(document_id)
        documents.append(DocumentSummary(document_id=document_id, title=title))
    return DocumentList(documents=documents)


@mcp.tool()
def search_clinic_knowledge(query: str, max_results: int = 3) -> SearchResults:
    """Semantically search clinic policy paragraphs using OpenAI and FAISS."""
    query = query.strip()
    if not query:
        raise ToolError("query must not be blank.")
    if not 1 <= max_results <= 10:
        raise ToolError("max_results must be between 1 and 10.")

    mode = os.getenv("CLINIC_KNOWLEDGE_SEARCH_MODE", "semantic").strip().lower()
    if mode == "lexical":
        matches = _lexical_matches(query, max_results)
    elif mode == "semantic":
        try:
            matches = _semantic_matches(query, max_results)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(
                "Semantic clinic knowledge search is temporarily unavailable."
            ) from exc
    else:
        raise ToolError(
            "CLINIC_KNOWLEDGE_SEARCH_MODE must be 'semantic' or 'lexical'."
        )

    return SearchResults(query=query, matches=matches)


@mcp.tool()
def read_knowledge_document(document_id: str) -> KnowledgeDocument:
    """Read one complete clinic policy document by its advertised ID."""
    title, content = _read_document(document_id)
    return KnowledgeDocument(
        document_id=document_id,
        title=title,
        content=content,
    )


if __name__ == "__main__":
    mcp.run()
