from enum import StrEnum

import jwt
from fastapi import Depends, Header, HTTPException, Request, status

from app.core.config import get_settings


class Role(StrEnum):
    ADMIN = "admin"
    REVIEWER = "reviewer"
    OFFICER = "officer"  # forest department officer / ranger (field responder)
    ANALYST = "analyst"  # environmental analyst
    READ_ONLY = "read_only"


def current_role(request: Request, x_role: str | None = Header(default=None), authorization: str | None = Header(default=None)) -> Role:
    """Resolve the caller role.

    ``jwt`` mode validates an HS256 bearer token (signature, expiry, audience) and
    reads the ``role`` claim; the X-Role header is ignored. ``dev_header`` mode is
    refused at startup in production.
    """
    settings = get_settings()
    if settings.auth_mode == "jwt":
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Bearer token required")
        try:
            claims = jwt.decode(authorization[7:], settings.jwt_secret, algorithms=["HS256"], audience=settings.jwt_audience, options={"require": ["exp", "sub", "aud"]})
            role = Role(str(claims.get("role", "")).lower())
        except (jwt.PyJWTError, ValueError) as exc:
            raise HTTPException(status_code=401, detail="Invalid token") from exc
        request.state.subject = str(claims["sub"])
        return role
    try:
        role = Role((x_role or Role.READ_ONLY).lower())
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Unknown role") from exc
    request.state.subject = f"dev:{role.value}"
    return role


def require_roles(*roles: Role):
    def dependency(role: Role = Depends(current_role)):
        if role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return role

    return dependency


def issue_token(subject: str, role: Role, ttl_seconds: int = 3600) -> str:
    """Helper for operators/tests; production tokens normally come from the identity provider."""
    import time

    settings = get_settings()
    now = int(time.time())
    return jwt.encode({"sub": subject, "role": role.value, "aud": settings.jwt_audience, "iat": now, "exp": now + ttl_seconds}, settings.jwt_secret, algorithm="HS256")
