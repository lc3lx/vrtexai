"""Sign in, refresh, and account self-service."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, EmailStr, Field

from app.api.deps import client_ip, current_user
from app.core.config import get_settings
from app.core.errors import AppError, account_disabled, bad_credentials
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.entities import AuditLog, Role, User, as_utc

logger = logging.getLogger("excelclear.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    role: Role
    display_name: str
    email: EmailStr


class RefreshRequest(BaseModel):
    refresh_token: str


class ChangePassword(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=256)


class Profile(BaseModel):
    id: str
    email: EmailStr
    role: Role
    display_name: str
    organisation: str
    monthly_quota: int
    used_this_month: int
    quota_remaining: int


def _profile(user: User) -> Profile:
    return Profile(
        id=str(user.id),
        email=user.email,
        role=user.role,
        display_name=user.display_name,
        organisation=user.organisation,
        monthly_quota=user.monthly_quota,
        used_this_month=user.used_this_month,
        quota_remaining=user.quota_remaining,
    )


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, request: Request) -> TokenPair:
    settings = get_settings()
    user = await User.find_one(User.email == payload.email.lower())

    if user is None:
        raise bad_credentials()
    now = datetime.now(timezone.utc)
    # as_utc, not the raw field: Mongo hands the timestamp back without a time
    # zone, and comparing that to an aware "now" raises — which would turn a
    # locked account into a 500 at the exact moment it must explain itself.
    locked_until = as_utc(user.locked_until)
    if locked_until and locked_until > now:
        minutes = int((locked_until - now).total_seconds() // 60) + 1
        raise AppError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "account_locked",
            f"too many attempts — try again in {minutes} minute(s)",
            minutes=minutes,
        )
    if not verify_password(payload.password, user.password_hash):
        user.failed_logins += 1
        if user.failed_logins >= settings.max_login_attempts:
            user.locked_until = now + timedelta(minutes=settings.lockout_minutes)
            user.failed_logins = 0
            logger.warning("account locked after repeated failures: %s", user.email)
        await user.save()
        raise bad_credentials()
    if not user.active:
        raise account_disabled()

    user.failed_logins = 0
    user.locked_until = None
    user.last_seen = now
    await user.save()
    await AuditLog(
        actor_id=str(user.id), actor_email=user.email, action="login", ip=client_ip(request)
    ).insert()

    return TokenPair(
        access_token=create_access_token(str(user.id), user.role.value, user.admin_id),
        refresh_token=create_refresh_token(str(user.id)),
        role=user.role,
        display_name=user.display_name or user.email,
        email=user.email,
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest) -> TokenPair:
    try:
        claims = decode_token(payload.refresh_token, expected_type="refresh")
    except ValueError as error:
        raise AppError(
            status.HTTP_401_UNAUTHORIZED, "session_expired", str(error)
        ) from error
    user = await User.get(claims.get("sub", ""))
    if user is None or not user.active:
        raise AppError(status.HTTP_401_UNAUTHORIZED, "session_expired", "account unavailable")
    return TokenPair(
        access_token=create_access_token(str(user.id), user.role.value, user.admin_id),
        refresh_token=create_refresh_token(str(user.id)),
        role=user.role,
        display_name=user.display_name or user.email,
        email=user.email,
    )


@router.get("/me", response_model=Profile)
async def me(user: User = Depends(current_user)) -> Profile:
    return _profile(user)


@router.post(
    "/password",
    status_code=status.HTTP_204_NO_CONTENT,
    # 204 means "done, nothing to send". Both of these are needed: FastAPI
    # otherwise derives a response model from the return annotation, and a
    # response model on a 204 is rejected when the route is registered.
    response_class=Response,
    response_model=None,
)
async def change_password(
    payload: ChangePassword, request: Request, user: User = Depends(current_user)
):
    if not verify_password(payload.current_password, user.password_hash):
        raise AppError(
            status.HTTP_400_BAD_REQUEST, "wrong_password", "current password is incorrect"
        )
    user.password_hash = hash_password(payload.new_password)
    await user.save()
    await AuditLog(
        actor_id=str(user.id),
        actor_email=user.email,
        action="password_changed",
        ip=client_ip(request),
    ).insert()
