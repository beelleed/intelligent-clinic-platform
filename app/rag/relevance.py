import os

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")

if not api_key:
    raise ValueError("OPENAI_API_KEY is not set.")

client = OpenAI(api_key=api_key)


def check_relevance(question, context):
    prompt = f"""
You are evaluating whether a retrieved clinic document passage
contains enough information to help answer the user's question.

Question:
{question}

Document passage:
{context}

Return exactly one word:

RELEVANT

or

NOT_RELEVANT

Return RELEVANT only when the passage directly contains information
that helps answer the question. Do not infer missing facts.
"""

    response = client.responses.create(
        model="gpt-5-mini",
        input=prompt
    )

    result = response.output_text.strip().upper()

    if result not in {"RELEVANT", "NOT_RELEVANT"}:
        raise ValueError(
            f"Unexpected relevance result: {result}"
        )

    return result

if __name__ == "__main__":
    test_question = (
        "What should staff do if a procedure takes longer than expected?"
    )

    test_context = """
    If the procedure takes longer than expected,
    clinic staff can extend the estimated completion
    time by 15, 30, or 60 minutes.

    The estimated completion time is only an approximation
    and may change depending on the actual procedure.
    """

    result = check_relevance(
        test_question,
        test_context
    )

    print(f"Question: {test_question}")
    print(f"Result: {result}")