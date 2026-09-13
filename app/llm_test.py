import os

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")

if not api_key:
    raise ValueError("OPENAI_API_KEY is not set.")

client = OpenAI(api_key=api_key)

response = client.responses.create(
    model="gpt-5-mini",
    input="""
Explain Retrieval-Augmented Generation (RAG).

Return exactly these three sections:

Definition:
Why it is useful:
Main limitation:
"""
)

print(response.output_text)