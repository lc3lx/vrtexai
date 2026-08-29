"""MongoDB documents.

Tenancy is modelled explicitly rather than implied. Every record a customer can
reach carries ``customer_id`` and ``admin_id``, and every query filters on them —
so an object belonging to someone else is not merely hidden from the interface,
it is unreachable through the API. That is the difference between a UI that does
not show a link and a system that cannot serve the data.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

import pymongo
from beanie import Document, Indexed
from pydantic import BaseModel, EmailStr, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """A stored timestamp, made safe to compare against ``_now()``.

    BSON has no time zone, so a datetime written as aware comes back naive.
    Subtracting one of those from an aware "now" raises, which turns a routine
    status poll into a 500 — and only ever on a job that is actually running,
    which is exactly when nobody can afford it. Everything is stored as UTC, so
    a naive value is simply relabelled rather than converted.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class Role(str, Enum):
    ADMIN = "admin"
    CUSTOMER = "customer"


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobKind(str, Enum):
    """What a job was asked to do.

    The two are priced differently because they cost differently. Reading a
    photographed invoice calls a vision model; tidying a spreadsheet the
    customer already has is arithmetic and string work on our own machine, so it
    spends no quota and is never rate limited.
    """

    EXTRACT = "extract"
    CLEAN = "clean"


class Stage(str, Enum):
    """The real stages of the pipeline, reported as they actually happen.

    There is no synthetic percentage here on purpose: the vision model does not
    report progress, and an invented number is worse than an honest wait.

    A cleaning job walks a shorter path — ``upload`` then ``clean`` — because
    there is no page to read and nothing for a model to say.
    """

    UPLOAD = "upload"
    EVIDENCE_OCR = "evidence_ocr"
    AI_VISION = "ai_vision"
    VERIFICATION = "verification"
    EXCEL = "excel"
    CLEAN = "clean"


# The stages each kind of job actually passes through, in order.
STAGES_BY_KIND: dict[JobKind, tuple[Stage, ...]] = {
    JobKind.EXTRACT: (
        Stage.UPLOAD, Stage.EVIDENCE_OCR, Stage.AI_VISION, Stage.VERIFICATION, Stage.EXCEL,
    ),
    JobKind.CLEAN: (Stage.UPLOAD, Stage.CLEAN),
}


class User(Document):
    email: Indexed(EmailStr, unique=True)  # type: ignore[valid-type]
    password_hash: str
    role: Role = Role.CUSTOMER
    display_name: str = ""
    organisation: str = ""
    active: bool = True

    # Who created this account. Customers always belong to exactly one admin;
    # admins have no parent.
    admin_id: str | None = None

    monthly_quota: int = 500
    used_this_month: int = 0
    quota_period: str = ""  # "YYYY-MM", so the counter resets by comparison

    failed_logins: int = 0
    locked_until: datetime | None = None
    last_seen: datetime | None = None
    created_at: datetime = Field(default_factory=_now)

    class Settings:
        name = "users"
        indexes = [
            [("admin_id", pymongo.ASCENDING), ("active", pymongo.ASCENDING)],
        ]

    @property
    def quota_remaining(self) -> int:
        return max(0, self.monthly_quota - self.used_this_month)


class StageTiming(BaseModel):
    stage: Stage
    ms: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    detail: str = ""


class FlaggedValue(BaseModel):
    """A value that failed a gate and was kept anyway.

    Never deleted: a figure the model read from a region the evidence reader
    missed is more useful highlighted than absent, and the reviewer decides.
    """

    cell: str
    value: Any = None
    reason: str
    gate: str


class Job(Document):
    # Tenancy, denormalised onto every job so an ownership check is one filter
    # and never a join that someone forgets to write.
    customer_id: Indexed(str)  # type: ignore[valid-type]
    admin_id: str | None = None

    kind: JobKind = JobKind.EXTRACT
    filename: str
    stored_name: str = ""
    content_type: str = ""
    size_bytes: int = 0
    page_count: int = 0

    status: JobStatus = JobStatus.QUEUED
    stage: Stage | None = None
    stages: list[StageTiming] = Field(default_factory=list)

    # Written by the reader as it works, not once at the end. Without these the
    # interface can only show "something is happening" for the whole run, which
    # is indistinguishable from a hang for the person waiting.
    stage_started_at: datetime | None = None
    pages_done: int = 0

    items_extracted: int = 0
    flagged: list[FlaggedValue] = Field(default_factory=list)
    result_path: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    error_code: str = ""

    ai_provider: str = ""
    ai_model: str = ""

    created_at: datetime = Field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    class Settings:
        name = "jobs"
        indexes = [
            [("customer_id", pymongo.ASCENDING), ("created_at", pymongo.DESCENDING)],
            [("status", pymongo.ASCENDING), ("created_at", pymongo.ASCENDING)],
        ]

    @property
    def total_ms(self) -> int:
        return sum(entry.ms for entry in self.stages)

    @property
    def flagged_count(self) -> int:
        return len(self.flagged)


class Period(str, Enum):
    """How often a plan is billed. Stored, never inferred from the price."""

    MONTHLY = "monthly"
    SEMIANNUAL = "semiannual"
    ANNUAL = "annual"


class Plan(Document):
    """A commercial package, as shown on the public page.

    Kept out of :class:`User` on purpose. A plan is a price list entry the
    administrator edits freely; a customer's ``monthly_quota`` is an operational
    limit the pipeline enforces. Tying them would mean an edit to marketing copy
    could silently cut off a running customer.
    """

    slug: Indexed(str, unique=True)  # type: ignore[valid-type]
    name_ar: str = ""
    name_en: str = ""
    price_amount: int = 0
    currency: str = "USD"
    period: Period = Period.MONTHLY
    monthly_limit: int = 0
    features_ar: list[str] = Field(default_factory=list)
    features_en: list[str] = Field(default_factory=list)
    # Cleaning a spreadsheet runs no model, so the monthly limit above does not
    # apply to it. Kept as a per-plan switch rather than a constant because it is
    # the administrator's to advertise, not ours to assume.
    cleaning_unlimited: bool = True
    highlighted: bool = False
    sort_order: int = 0
    active: bool = True
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    class Settings:
        name = "plans"
        indexes = [[("sort_order", pymongo.ASCENDING)]]


class LeadStatus(str, Enum):
    NEW = "new"
    CONTACTED = "contacted"
    CONVERTED = "converted"
    REJECTED = "rejected"


class Lead(Document):
    """A subscription request from the public page.

    There is no self-service signup: the request carries a WhatsApp number, an
    administrator gets in touch, and only then is an account created. The plan
    name is snapshotted because prices are edited and the request should still
    say what was on the page when it was sent.
    """

    full_name: str
    whatsapp: Indexed(str)  # type: ignore[valid-type]
    plan_slug: str = ""
    plan_name: str = ""
    note: str = ""
    status: LeadStatus = LeadStatus.NEW
    ip: str = ""
    created_at: datetime = Field(default_factory=_now)
    contacted_at: datetime | None = None

    class Settings:
        name = "leads"
        indexes = [
            [("status", pymongo.ASCENDING), ("created_at", pymongo.DESCENDING)],
        ]


class AuditLog(Document):
    """Who did what. Written for actions that change access or spend quota."""

    actor_id: str
    actor_email: str = ""
    action: Indexed(str)  # type: ignore[valid-type]
    target_id: str = ""
    detail: str = ""
    ip: str = ""
    at: datetime = Field(default_factory=_now)

    class Settings:
        name = "audit_logs"
        indexes = [[("at", pymongo.DESCENDING)]]


ALL_DOCUMENTS = [User, Job, AuditLog, Plan, Lead]
