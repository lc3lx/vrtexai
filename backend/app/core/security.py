"""Passwords and tokens.

Two decisions worth stating, because both are easy to get subtly wrong:

* Passwords are hashed with bcrypt, never stored or logged in any recoverable
  form. A leaked database must not become a leaked customer list.
* Access tokens are short-lived and carry the role and tenancy, but the server
  still re-checks ownership on every request. A token says who you claim to be;
  it is not permission to touch a particular document.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import jwt
from passlib.context import CryptContext

from app.core.config import get_settings

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

Role = Literal["admin", "customer"]


def hash_password(password: str) -> str:
    return _pwd.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _pwd.verify(password, hashed)
    except Exception:
        # A malformed stored hash must read as "wrong password", never as an
        # exception that a caller might mistake for success.
        return False


def generate_password(length: int = 14) -> str:
    """A password an admin can hand to a new customer once."""
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_access_token(subject: str, role: Role, tenant: str | None = None) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_minutes),
    }
    if tenant:
        payload["tenant"] = tenant
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_refresh_token(subject: str) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "type": "refresh",
        "iat": now,
        "exp": now + timedelta(days=settings.refresh_token_days),
        # A unique id so a refresh token can be revoked individually.
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, expected_type: str = "access") -> dict[str, Any]:
    """Claims from a valid token. Raises ValueError on anything else."""
    settings = get_settings()
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as error:
        raise ValueError("token expired") from error
    except jwt.PyJWTError as error:
        raise ValueError("invalid token") from error
    # Checked explicitly: a refresh token presented as an access token would
    # otherwise grant a session that outlives its intended lifetime by weeks.
    if claims.get("type") != expected_type:
        raise ValueError(f"expected a {expected_type} token")
    return claims
