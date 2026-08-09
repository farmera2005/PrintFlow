"""Single-admin local login: bcrypt password, signed session cookie."""

from __future__ import annotations

import uuid

import bcrypt
from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_config
from .db import get_session
from .models import User

_SALT = "printflow-session-v1"

# bcrypt silently ignores everything past 72 bytes; reject instead of pretending.
MAX_PASSWORD_BYTES = 72


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_config().secret_key, salt=_SALT)


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError("Password must be 72 bytes or fewer.")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def issue_session(response: Response, user: User, request: Request | None = None) -> None:
    token = _serializer().dumps({"uid": str(user.id)})
    config = get_config()
    # Mark the cookie Secure whenever the session was established over HTTPS —
    # which is always the case through a Cloudflare tunnel. Doing it
    # unconditionally would break sign-in over the plain-HTTP fallback port.
    secure = bool(request is not None and request.url.scheme == "https")
    response.set_cookie(
        config.session_cookie,
        token,
        max_age=config.session_max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(get_config().session_cookie, path="/")


async def current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User | None:
    config = get_config()
    token = request.cookies.get(config.session_cookie)
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=config.session_max_age)
    except (BadSignature, SignatureExpired):
        return None
    raw_uid = data.get("uid")
    try:
        uid = uuid.UUID(str(raw_uid))
    except (TypeError, ValueError):
        return None
    return await session.get(User, uid)


async def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    return user
