import os
import pickle

import faiss
import numpy as np

from app.rag.chunker import chunk_text
from app.rag.embeddings import create_embedding
from app.rag.loader import load_text_file


INDEX_PATH = "data/vector_store.index"
METADATA_PATH = "data/vector_metadata.pkl"


def build_vector_store():
    text = load_text_file("data/clinic_faq.txt")

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
        INDEX_PATH
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