import os

from dotenv import load_dotenv
from openai import OpenAI


def main() -> None:
    """Run the manual provider smoke test without side effects on import."""
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not set.")

    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=os.getenv("OPENAI_MODEL", "gpt-5-nano"),
        reasoning={
            "effort": os.getenv("OPENAI_REASONING_EFFORT", "minimal")
        },
        input="""
Explain Retrieval-Augmented Generation (RAG).

Return exactly these three sections:

Definition:
Why it is useful:
Main limitation:
""",
    )
    print(response.output_text)


if __name__ == "__main__":
    main()
