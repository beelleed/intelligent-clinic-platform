from app.rag.loader import load_text_file


def chunk_text(text):
    paragraphs = [
        paragraph.strip()
        for paragraph in text.split("\n\n")
        if paragraph.strip()
    ]

    chunks = []

    for paragraph in paragraphs:
        chunks.append(
            {
                "text": paragraph,
                "source": "clinic_faq.txt",
                "chunk_id": len(chunks) + 1
            }
        )

    return chunks


if __name__ == "__main__":
    text = load_text_file("data/clinic_faq.txt")

    chunks = chunk_text(text)

    print(f"Total chunks: {len(chunks)}")

    for chunk in chunks:
        print(f"\n--- Chunk {chunk['chunk_id']} ---")
        print(f"Source: {chunk['source']}")
        print(chunk["text"])