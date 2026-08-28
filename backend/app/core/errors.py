"""Errors the browser can say in the reader's own language.

An API that returns ``"email or password is incorrect"`` has already decided
which language the user reads. This product is sold into an Arabic market and
its interface flips between Arabic and English at a click, so the server sends a
*code* and the browser chooses the words:

    {"detail": {"code": "bad_credentials", "message": "...", "params": {}}}

``message`` is the English fallback — it keeps the API honest for anyone reading
it with curl, and it is what the browser shows if it meets a code it does not
know yet. ``params`` carries the numbers a sentence needs (a limit, a count, a
number of minutes) so the translation can put them where its own grammar wants
them rather than where English happened to.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(HTTPException):
    """An HTTP error carrying a stable code alongside its English text."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        **params: Any,
    ) -> None:
        super().__init__(
            status_code,
            {"code": code, "message": message, "params": params},
        )
        self.code = code


def bad_credentials() -> AppError:
    # One code and one message for every sign-in failure. Distinguishing "no
    # such account" from "wrong password" hands an attacker a customer list.
    return AppError(
        status.HTTP_401_UNAUTHORIZED, "bad_credentials", "email or password is incorrect"
    )


def signin_required() -> AppError:
    return AppError(status.HTTP_401_UNAUTHORIZED, "signin_required", "sign in to continue")


def account_disabled() -> AppError:
    return AppError(status.HTTP_403_FORBIDDEN, "account_disabled", "this account is disabled")


def not_found(code: str, message: str) -> AppError:
    return AppError(status.HTTP_404_NOT_FOUND, code, message)


async def validation_handler(request: Request, error: RequestValidationError) -> JSONResponse:
    """Turn FastAPI's field-by-field report into one coded message.

    The raw 422 body is a list of pydantic error objects naming internal field
    paths. Useful in a log, meaningless in a form, and impossible to translate —
    so the field name is passed as a parameter and the sentence is the browser's
    to write.
    """
    first = (error.errors() or [{}])[0]
    location = [part for part in first.get("loc", []) if part not in ("body", "query", "path")]
    field = str(location[-1]) if location else ""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": {
                "code": "invalid_field",
                "message": f"{field or 'a value'} is not valid: {first.get('msg', 'invalid')}",
                "params": {"field": field},
            }
        },
    )
