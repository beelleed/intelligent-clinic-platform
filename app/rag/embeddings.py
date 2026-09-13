import os

from dotenv import load_dotenv
from openai import OpenAI

from app.rag.loader import load_text_file
from app.rag.chunker import chunk_text


load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")

if not api_key:
    raise ValueError("OPENAI_API_KEY is not set.")

client = OpenAI(api_key=api_key)


def create_embedding(text):
    response = client.embeddings.create(
        model="text-embedding-3-small",
        input=text
    )

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