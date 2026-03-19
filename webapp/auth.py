"""Session-based auth for all protected routes."""
import os
import secrets

from fastapi import Request


class NotAuthenticatedException(Exception):
    """Raised by require_auth when no valid session is present."""


def require_auth(request: Request) -> str:
    """FastAPI dependency — raises NotAuthenticatedException if not logged in."""
    user = request.session.get("user")
    if not user:
        raise NotAuthenticatedException()
    return user


def check_credentials(username: str, password: str) -> bool:
    """Timing-safe credential check. Returns False if AUTH_PASS is unset."""
    valid_user = os.environ.get("AUTH_USER", "admin")
    valid_pass = os.environ.get("AUTH_PASS", "")
    if not valid_pass:
        return False
    return secrets.compare_digest(
        username.encode("utf-8"), valid_user.encode("utf-8")
    ) and secrets.compare_digest(
        password.encode("utf-8"), valid_pass.encode("utf-8")
    )
