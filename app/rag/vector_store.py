import os
import pickle
from pathlib import Path

import faiss
import numpy as np

from app.rag.chunker import chunk_text
from app.rag.embeddings import create_embedding
from app.rag.loader import load_text_file


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIRECTORY = PROJECT_ROOT / "data"
INDEX_PATH = DATA_DIRECTORY / "vector_store.index"
METADATA_PATH = DATA_DIRECTORY / "vector_metadata.pkl"
KNOWLEDGE_PATH = DATA_DIRECTORY / "clinic_faq.txt"


def build_vector_store():
    DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
    text = load_text_file(KNOWLEDGE_PATH)

    chunks = chunk_text(text)

    embeddings = []

    for chunk in chunks:
        embedding = create_embedding(chunk["text"])
        embeddings.append(embedding)

    embeddings = np.array(
        embeddings,
        dtype="float32"
    )

    dimension = embeddings.shape[1]

    faiss.normalize_L2(embeddings)

    index = faiss.IndexFlatIP(dimension)
    index.add(embeddings)

    metadata = chunks

    faiss.write_index(
        index,
        str(INDEX_PATH)
    )

    with open(
        METADATA_PATH,
        "wb"
    ) as file:
        pickle.dump(
            metadata,
            file
        )

    print(
        f"Stored {index.ntotal} vectors."
    )

    print(
        f"Vector dimension: {dimension}"
    )


def vector_store_exists():
    return (
        os.path.exists(INDEX_PATH)
        and os.path.exists(METADATA_PATH)
    )


def initialize_vector_store():
    if vector_store_exists():
        print("Vector store already exists.")
        return

    print("Vector store not found. Building...")
    build_vector_store()


if __name__ == "__main__":
    initialize_vector_store()
