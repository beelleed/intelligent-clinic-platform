from typing import Annotated

from pydantic import BaseModel, StringConstraints, Field


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
    cards: list[dict] = Field(default_factory=list)
    available_dates: list[str] = Field(default_factory=list)
    selected_date: str | None = None
