"""The public surface: what an anonymous visitor may read and send.

Two endpoints, and only two. The price list is readable because it is printed on
the landing page anyway, and a subscription request is writable because there is
no other way for a prospect to reach us. Everything else in this API requires a
token, and nothing here exposes a customer, a job, or a document.

``POST /leads`` is the only unauthenticated write in the system, so it carries
its own defences: a strict schema, a honeypot field, and a per-address rate
limit. The limit lives in process memory on purpose — this deployment is a
single process, and reaching for Redis to slow down a form would be a service to
operate for no gain.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field, field_validator

from app.api.deps import client_ip
from app.core.errors import AppError
from app.models.entities import AuditLog, Lead, Plan

router = APIRouter(prefix="/api/public", tags=["public"])

LEAD_WINDOW_SECONDS = 3600
LEAD_MAX_PER_WINDOW = 5
_lead_hits: dict[str, deque[float]] = defaultdict(deque)

_DIGITS = re.compile(r"[^\d+]")
_E164 = re.compile(r"^\+?\d{8,15}$")


class PlanOut(BaseModel):
    slug: str
    name_ar: str
    name_en: str
    price_amount: int
    currency: str
    period: str
    monthly_limit: int
    features_ar: list[str]
    features_en: list[str]
    cleaning_unlimited: bool
    highlighted: bool


class LeadIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    whatsapp: str = Field(min_length=8, max_length=24)
    plan_slug: str = Field(default="", max_length=40)
    note: str = Field(default="", max_length=500)
    # Not shown to a human. A browser leaves it empty; most form bots fill every
    # input they find, and that is the whole tell.
    company: str = Field(default="", max_length=200)

    @field_validator("whatsapp")
    @classmethod
    def normalise_whatsapp(cls, value: str) -> str:
        cleaned = _DIGITS.sub("", value.strip())
        if not _E164.match(cleaned):
            raise ValueError("enter a valid WhatsApp number, digits only, with country code")
        return cleaned


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    hits = _lead_hits[ip]
    while hits and now - hits[0] > LEAD_WINDOW_SECONDS:
        hits.popleft()
    if len(hits) >= LEAD_MAX_PER_WINDOW:
        return True
    hits.append(now)
    return False


@router.get("/plans", response_model=list[PlanOut])
async def public_plans() -> list[PlanOut]:
    plans = await Plan.find(Plan.active == True).sort(+Plan.sort_order).to_list()  # noqa: E712
    return [
        PlanOut(
            slug=plan.slug,
            name_ar=plan.name_ar,
            name_en=plan.name_en,
            price_amount=plan.price_amount,
            currency=plan.currency,
            period=plan.period.value,
            monthly_limit=plan.monthly_limit,
            features_ar=plan.features_ar,
            features_en=plan.features_en,
            cleaning_unlimited=plan.cleaning_unlimited,
            highlighted=plan.highlighted,
        )
        for plan in plans
    ]


@router.post("/leads", response_model=dict, status_code=status.HTTP_201_CREATED)
async def submit_lead(payload: LeadIn, request: Request) -> dict:
    if payload.company:
        # Answer exactly as a success would, so a bot learns nothing from the
        # difference and does not come back with the field left blank.
        return {"ok": True}

    ip = client_ip(request)
    if _rate_limited(ip):
        raise AppError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "lead_rate_limited",
            "too many requests from this address. Try again later.",
        )

    plan = await Plan.find_one(Plan.slug == payload.plan_slug) if payload.plan_slug else None
    lead = Lead(
        full_name=payload.full_name.strip(),
        whatsapp=payload.whatsapp,
        plan_slug=plan.slug if plan else "",
        plan_name=plan.name_ar if plan else "",
        note=payload.note.strip(),
        ip=ip,
    )
    await lead.insert()
    await AuditLog(
        actor_id="public",
        actor_email="",
        action="lead_received",
        target_id=str(lead.id),
        detail=lead.plan_slug,
        ip=ip,
    ).insert()
    return {"ok": True}
