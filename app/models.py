from typing import Annotated

from pydantic import BaseModel, StringConstraints


QueryText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
]
SessionId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]


class ChatRequest(BaseModel):
    query: QueryText
    session_id: SessionId


class ChatResponse(BaseModel):
    response: str
