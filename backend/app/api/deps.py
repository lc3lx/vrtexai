"""Request dependencies: who is calling, and may they touch this object.

Authentication and authorisation are separated deliberately. A valid token
proves identity; it never proves entitlement to a particular document. Every
handler that reaches a record goes through :func:`owned_job`, so a customer id
in a URL cannot become a way to read someone else's invoice.
"""
from __future__ import annotations

from fastapi import Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.errors import AppError, account_disabled, not_found, signin_required
from app.core.security import decode_token
from app.models.entities import Job, Role, User

_bearer = HTTPBearer(auto_error=False)


async def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    if credentials is None:
        raise signin_required()
    try:
        claims = decode_token(credentials.credentials)
    except ValueError as error:
        raise AppError(
            status.HTTP_401_UNAUTHORIZED, "session_expired", str(error)
        ) from error

    user = await User.get(claims.get("sub", ""))
    if user is None:
        raise AppError(status.HTTP_401_UNAUTHORIZED, "session_expired", "account not found")
    # Checked on every request, not just at login: disabling an account has to
    # take effect immediately, not when the customer's token happens to expire.
    if not user.active:
        raise account_disabled()
    return user


async def current_admin(user: User = Depends(current_user)) -> User:
    if user.role != Role.ADMIN:
        raise AppError(
            status.HTTP_403_FORBIDDEN, "admin_only", "administrator access required"
        )
    return user


async def owned_job(job_id: str, user: User = Depends(current_user)) -> Job:
    """A job the caller is entitled to see, or 404.

    Deliberately 404 rather than 403 for someone else's job: telling a stranger
    that an id exists but is not theirs leaks the fact that it exists.
    """
    job = await Job.get(job_id)
    if job is None:
        raise not_found("job_not_found", "job not found")

    if user.role == Role.ADMIN:
        # An admin sees the jobs of customers they created — not every job in
        # the system, and never another admin's tenants.
        if job.admin_id == str(user.id):
            return job
        raise not_found("job_not_found", "job not found")

    if job.customer_id != str(user.id):
        raise not_found("job_not_found", "job not found")
    return job


async def managed_customer(customer_id: str, admin: User = Depends(current_admin)) -> User:
    """A customer this admin created."""
    customer = await User.get(customer_id)
    if customer is None or customer.role != Role.CUSTOMER or customer.admin_id != str(admin.id):
        raise not_found("customer_not_found", "customer not found")
    return customer


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""
