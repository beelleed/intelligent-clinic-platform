import pickle

import faiss
import numpy as np

from app.rag.embeddings import create_embedding


INDEX_PATH = "data/vector_store.index"
METADATA_PATH = "data/vector_metadata.pkl"


def search(query, top_k=2, min_similarity=None):
    index = faiss.read_index(INDEX_PATH)

    with open(METADATA_PATH, "rb") as file:
        metadata = pickle.load(file)

    query_embedding = create_embedding(query)

    query_vector = np.array(
        [query_embedding],
        dtype="float32"
    )

    faiss.normalize_L2(query_vector)

    scores, indices = index.search(
        query_vector,
        top_k
    )

    results = []

    for score, index_id in zip(
        scores[0],
        indices[0]
    ):
        if index_id == -1:
            continue

        if (
            min_similarity is not None
            and score < min_similarity
        ):
            continue

        results.append(
            {
                "score": float(score),
                "metadata": metadata[index_id]
            }
        )

    return results


if __name__ == "__main__":
    question = "What number does the clinic website display?"

    results = search(
        question,
        top_k=2,
        min_similarity=0.50
    )

    print("Question:")
    print(question)

    print("\nTop results:")

    for rank, result in enumerate(
        results,
        start=1
    ):
        metadata = result["metadata"]

        print(f"\n--- Rank {rank} ---")
        print(f"Chunk: {metadata['chunk_id']}")
        print(f"Source: {metadata['source']}")
        print(f"Similarity: {result['score']:.4f}")
        print(metadata["text"])