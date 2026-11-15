"""Anonymous reservation ownership, independent of the resettable chat session."""

from contextvars import ContextVar
import hashlib
import re
import secrets
from urllib.parse import urlparse

from fastapi import HTTPException, Request, Response


COOKIE_NAME = "lumi_booking_owner"
booking_owner_context: ContextVar[str | None] = ContextVar("booking_owner", default=None)


def browser_owner(request: Request, response: Response) -> str:
    token = request.cookies.get(COOKIE_NAME, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        token = secrets.token_urlsafe(32)
        response.set_cookie(COOKIE_NAME, token, max_age=31536000, httponly=True,
            secure=request.url.hostname not in {"localhost", "127.0.0.1", "testserver"},
            samesite="strict", path="/")
    return hashlib.sha256(token.encode()).hexdigest()


def check_booking_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin:
        parsed = urlparse(origin)
        local = request.url.hostname in {"localhost", "127.0.0.1", "testserver"}
        if parsed.netloc != request.url.netloc or parsed.scheme not in ({"http", "https"} if local else {"https"}):
            raise HTTPException(status_code=403, detail="Please manage reservations from this website.")
