from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    clear_session,
    current_user,
    issue_session,
    require_user,
    verify_password,
)
from ..db import get_session
from ..models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict:
    user = (
        await session.execute(select(User).where(User.username == body.username.strip()))
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        # Same message either way — don't leak whether the username exists.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect username or password")
    issue_session(response, user, request)
    return {"username": user.username}


@router.post("/logout")
async def logout(response: Response) -> dict:
    clear_session(response)
    return {"ok": True}


@router.get("/me")
async def me(user: User | None = Depends(current_user)) -> dict:
    if user is None:
        return {"authenticated": False}
    return {"authenticated": True, "username": user.username}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=72)


@router.post("/password")
async def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from ..auth import hash_password

    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")
    try:
        user.password_hash = hash_password(body.new_password)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await session.commit()
    return {"ok": True}
