import hmac
from collections.abc import Iterator

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def require_token(request: Request) -> None:
    """Enforce the optional bearer token (MINIHCI_API_TOKEN). Unset or empty means the API is open."""
    expected = request.app.state.settings.api_token
    if not expected:
        return
    scheme, _, given = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "missing or invalid API token", headers={"WWW-Authenticate": "Bearer"})
