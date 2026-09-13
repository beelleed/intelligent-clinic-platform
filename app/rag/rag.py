import os

from dotenv import load_dotenv
from openai import OpenAI

from app.rag.retriever import search


load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")

if not api_key:
    raise ValueError("OPENAI_API_KEY is not set.")

client = OpenAI(api_key=api_key)


def generate_answer(question, retrieved_results):
    context_parts = []

    for rank, result in enumerate(
        retrieved_results,
        start=1
    ):
        metadata = result["metadata"]

        context_parts.append(
            f"[Source {rank}]\n"
            f"Document: {metadata['source']}\n"
            f"Chunk: {metadata['chunk_id']}\n"
            f"Similarity: {result['score']:.4f}\n"
            f"{metadata['text']}"
        )

    context = "\n\n".join(context_parts)

    prompt = f"""
You are a clinic information assistant.

Answer the user's question using only the provided context.

Rules:
1. Do not invent clinic policies or procedures.
2. If the context does not contain enough information, say:
   "I don't have enough information in the provided clinic documents."
3. Keep the answer concise and clear.
4. Do not mention information that is not supported by the context.

Context:
{context}

Question:
{question}
"""

    response = client.responses.create(
        model="gpt-5-mini",
        input=prompt
    )

    return response.output_text


if __name__ == "__main__":
    question = "What number does the clinic website display?"

    retrieved_results = search(
        question,
        top_k=2
    )

    MIN_SIMILARITY = 0.50

    relevant_results = [
        result
        for result in retrieved_results
        if result["score"] >= MIN_SIMILARITY
    ]

    answer = generate_answer(
        question,
        relevant_results
    )

    print("Question:")
    print(question)

    print("\nAnswer:")
    print(answer)

    print("\nSources:")

    if not relevant_results:
        print("No relevant source found.")
    else:
        for rank, result in enumerate(
            relevant_results,
            start=1
        ):
            metadata = result["metadata"]

            print(
                f"{rank}. "
                f"{metadata['source']} "
                f"(Chunk {metadata['chunk_id']}, "
                f"similarity={result['score']:.4f})"
            )