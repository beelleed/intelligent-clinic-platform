import os

from dotenv import load_dotenv
from openai import OpenAI

from app.rag.loader import load_text_file
from app.rag.chunker import chunk_text


load_dotenv()

def _create_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not set.")
    return OpenAI(api_key=api_key)


def create_embeddings(texts: list[str]) -> list[list[float]]:
    """Create embeddings in one request for efficient index construction."""
    if not texts:
        return []

    response = _create_client().embeddings.create(
        model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
        input=texts,
    )
    return [item.embedding for item in sorted(response.data, key=lambda item: item.index)]


def create_embedding(text):
    return create_embeddings([text])[0]

    return response.data[0].embedding


if __name__ == "__main__":
    text = load_text_file("data/clinic_faq.txt")

    chunks = chunk_text(
        text,
        source="clinic_faq.txt"
    )

    for chunk in chunks:
        embedding = create_embedding(chunk["text"])

        print(f"Chunk {chunk['chunk_id']}")
        print(f"Source: {chunk['source']}")
        print(f"Embedding dimensions: {len(embedding)}")
        print()
