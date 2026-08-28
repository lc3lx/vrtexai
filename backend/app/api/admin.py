"""Administrator console: create and manage the customers you own."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, EmailStr, Field

from app.api.deps import client_ip, current_admin, managed_customer
from app.core.errors import AppError, not_found
from app.core.security import generate_password, hash_password
from app.models.entities import (
    AuditLog, Job, JobStatus, Lead, LeadStatus, Period, Plan, Role, User,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


class CreateCustomer(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=120)
    organisation: str = Field(default="", max_length=160)
    monthly_quota: int = Field(default=500, ge=0, le=1_000_000)


class UpdateCustomer(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    organisation: str | None = Field(default=None, max_length=160)
    monthly_quota: int | None = Field(default=None, ge=0, le=1_000_000)
    active: bool | None = None


class CustomerOut(BaseModel):
    id: str
    email: EmailStr
    display_name: str
    organisation: str
    active: bool
    monthly_quota: int
    used_this_month: int
    last_seen: datetime | None
    created_at: datetime


class CustomerCreated(CustomerOut):
    # Returned once, at creation, so the admin can hand it over. It is never
    # readable again — only resettable.
    password: str


def _out(user: User) -> CustomerOut:
    return CustomerOut(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        organisation=user.organisation,
        active=user.active,
        monthly_quota=user.monthly_quota,
        used_this_month=user.used_this_month,
        last_seen=user.last_seen,
        created_at=user.created_at,
    )


@router.get("/customers", response_model=list[CustomerOut])
async def list_customers(admin: User = Depends(current_admin)) -> list[CustomerOut]:
    customers = await User.find(User.admin_id == str(admin.id)).to_list()
    return [_out(customer) for customer in customers]


async def _provision_customer(
    admin: User,
    request: Request,
    *,
    email: str,
    display_name: str,
    organisation: str,
    monthly_quota: int,
) -> tuple[User, str]:
    """Create one customer account under this admin, returning it and its password.

    Shared by the customers form and by converting a subscription request, so
    both routes get the same tenancy link, the same audit entry, and the same
    once-only password.
    """
    email = email.lower()
    if await User.find_one(User.email == email):
        raise AppError(status.HTTP_409_CONFLICT, "email_taken",
                       "an account with this email already exists")

    password = generate_password()
    customer = User(
        email=email,
        password_hash=hash_password(password),
        role=Role.CUSTOMER,
        display_name=display_name,
        organisation=organisation,
        monthly_quota=monthly_quota,
        # The tenancy link. Every job this customer creates inherits it, which
        # is what keeps one admin's customers invisible to another's.
        admin_id=str(admin.id),
    )
    await customer.insert()
    await AuditLog(
        actor_id=str(admin.id),
        actor_email=admin.email,
        action="customer_created",
        target_id=str(customer.id),
        detail=email,
        ip=client_ip(request),
    ).insert()
    return customer, password


@router.post("/customers", response_model=CustomerCreated, status_code=status.HTTP_201_CREATED)
async def create_customer(
    payload: CreateCustomer, request: Request, admin: User = Depends(current_admin)
) -> CustomerCreated:
    customer, password = await _provision_customer(
        admin,
        request,
        email=payload.email,
        display_name=payload.display_name,
        organisation=payload.organisation,
        monthly_quota=payload.monthly_quota,
    )
    return CustomerCreated(**_out(customer).model_dump(), password=password)


@router.patch("/customers/{customer_id}", response_model=CustomerOut)
async def update_customer(
    payload: UpdateCustomer,
    request: Request,
    customer: User = Depends(managed_customer),
    admin: User = Depends(current_admin),
) -> CustomerOut:
    changes = payload.model_dump(exclude_none=True)
    for field, value in changes.items():
        setattr(customer, field, value)
    await customer.save()
    await AuditLog(
        actor_id=str(admin.id),
        actor_email=admin.email,
        action="customer_updated",
        target_id=str(customer.id),
        detail=", ".join(f"{k}={v}" for k, v in changes.items()),
        ip=client_ip(request),
    ).insert()
    return _out(customer)


@router.post("/customers/{customer_id}/password", response_model=dict)
async def reset_password(
    request: Request,
    customer: User = Depends(managed_customer),
    admin: User = Depends(current_admin),
) -> dict:
    password = generate_password()
    customer.password_hash = hash_password(password)
    customer.failed_logins = 0
    customer.locked_until = None
    await customer.save()
    await AuditLog(
        actor_id=str(admin.id),
        actor_email=admin.email,
        action="password_reset",
        target_id=str(customer.id),
        ip=client_ip(request),
    ).insert()
    return {"password": password}


@router.get("/jobs", response_model=list[dict])
async def all_jobs(limit: int = 100, admin: User = Depends(current_admin)) -> list[dict]:
    """Jobs across this admin's customers — never another tenant's."""
    jobs = (
        await Job.find(Job.admin_id == str(admin.id))
        .sort(-Job.created_at)
        .limit(min(limit, 500))
        .to_list()
    )
    owners = {
        str(user.id): user.display_name or user.email
        for user in await User.find(User.admin_id == str(admin.id)).to_list()
    }
    return [
        {
            "id": str(job.id),
            "filename": job.filename,
            "customer": owners.get(job.customer_id, "—"),
            "status": job.status,
            "items": job.items_extracted,
            "flagged": job.flagged_count,
            "ai_provider": job.ai_provider,
            "total_ms": job.total_ms,
            "created_at": job.created_at,
        }
        for job in jobs
    ]


@router.get("/usage", response_model=list[dict])
async def usage(admin: User = Depends(current_admin)) -> list[dict]:
    customers = await User.find(User.admin_id == str(admin.id)).to_list()
    return [
        {
            "id": str(c.id),
            "name": c.display_name or c.email,
            "used": c.used_this_month,
            "quota": c.monthly_quota,
            "active": c.active,
        }
        for c in customers
    ]


@router.get("/system", response_model=dict)
async def system_status(admin: User = Depends(current_admin)) -> dict:
    from app.services.ai_provider import build_provider
    from app.core.config import get_settings

    settings = get_settings()
    provider = build_provider(settings)
    ready, detail = provider.health()
    queued = await Job.find(Job.status == JobStatus.QUEUED).count()
    running = await Job.find(Job.status == JobStatus.PROCESSING).count()
    return {
        "ai_ready": ready,
        "ai_detail": detail,
        "ai_provider": settings.ai_provider,
        "fallback_local": settings.ai_fallback_local,
        "queued": queued,
        "processing": running,
        "database": f"MongoDB · {settings.mongo_db}",
        "checked_at": datetime.now(timezone.utc),
    }


# --------------------------------------------------------------------------
# Plans — the price list behind the public page.
#
# System-wide rather than per-admin: there is one public page, so there is one
# price list. Editing a plan changes what visitors are quoted; it never touches
# a customer's quota, which stays an operational setting on the account.
# --------------------------------------------------------------------------


class PlanIn(BaseModel):
    slug: str = Field(min_length=2, max_length=40, pattern=r"^[a-z0-9-]+$")
    name_ar: str = Field(min_length=1, max_length=80)
    name_en: str = Field(default="", max_length=80)
    price_amount: int = Field(ge=0, le=1_000_000)
    currency: str = Field(default="USD", min_length=1, max_length=8)
    period: Period = Period.MONTHLY
    monthly_limit: int = Field(ge=0, le=10_000_000)
    features_ar: list[str] = Field(default_factory=list, max_length=12)
    features_en: list[str] = Field(default_factory=list, max_length=12)
    highlighted: bool = False
    sort_order: int = Field(default=0, ge=0, le=999)
    active: bool = True


class PlanUpdate(BaseModel):
    name_ar: str | None = Field(default=None, max_length=80)
    name_en: str | None = Field(default=None, max_length=80)
    price_amount: int | None = Field(default=None, ge=0, le=1_000_000)
    currency: str | None = Field(default=None, max_length=8)
    period: Period | None = None
    monthly_limit: int | None = Field(default=None, ge=0, le=10_000_000)
    features_ar: list[str] | None = Field(default=None, max_length=12)
    features_en: list[str] | None = Field(default=None, max_length=12)
    highlighted: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=999)
    active: bool | None = None


def _plan_out(plan: Plan) -> dict:
    return {
        "id": str(plan.id),
        "slug": plan.slug,
        "name_ar": plan.name_ar,
        "name_en": plan.name_en,
        "price_amount": plan.price_amount,
        "currency": plan.currency,
        "period": plan.period,
        "monthly_limit": plan.monthly_limit,
        "features_ar": plan.features_ar,
        "features_en": plan.features_en,
        "highlighted": plan.highlighted,
        "sort_order": plan.sort_order,
        "active": plan.active,
        "updated_at": plan.updated_at,
    }


async def _plan_or_404(plan_id: str) -> Plan:
    plan = await Plan.get(plan_id)
    if plan is None:
        raise not_found("plan_not_found", "plan not found")
    return plan


@router.get("/plans", response_model=list[dict])
async def list_plans(admin: User = Depends(current_admin)) -> list[dict]:
    plans = await Plan.find_all().sort(+Plan.sort_order).to_list()
    return [_plan_out(plan) for plan in plans]


@router.post("/plans", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_plan(
    payload: PlanIn, request: Request, admin: User = Depends(current_admin)
) -> dict:
    if await Plan.find_one(Plan.slug == payload.slug):
        raise AppError(status.HTTP_409_CONFLICT, "plan_slug_taken",
                       "a plan with this slug already exists")
    plan = Plan(**payload.model_dump())
    await plan.insert()
    await AuditLog(
        actor_id=str(admin.id),
        actor_email=admin.email,
        action="plan_created",
        target_id=str(plan.id),
        detail=plan.slug,
        ip=client_ip(request),
    ).insert()
    return _plan_out(plan)


@router.patch("/plans/{plan_id}", response_model=dict)
async def update_plan(
    plan_id: str,
    payload: PlanUpdate,
    request: Request,
    admin: User = Depends(current_admin),
) -> dict:
    plan = await _plan_or_404(plan_id)
    changes = payload.model_dump(exclude_none=True)
    for field, value in changes.items():
        setattr(plan, field, value)
    plan.updated_at = datetime.now(timezone.utc)
    await plan.save()
    await AuditLog(
        actor_id=str(admin.id),
        actor_email=admin.email,
        action="plan_updated",
        target_id=str(plan.id),
        detail=", ".join(f"{k}={v}" for k, v in changes.items())[:400],
        ip=client_ip(request),
    ).insert()
    return _plan_out(plan)


@router.delete("/plans/{plan_id}", response_model=dict)
async def retire_plan(
    plan_id: str, request: Request, admin: User = Depends(current_admin)
) -> dict:
    """Retire a plan by deactivating it.

    Never a real delete: leads already carry a snapshot of the plan they asked
    about, and removing the row would strand a request nobody could interpret.
    """
    plan = await _plan_or_404(plan_id)
    plan.active = False
    plan.updated_at = datetime.now(timezone.utc)
    await plan.save()
    await AuditLog(
        actor_id=str(admin.id),
        actor_email=admin.email,
        action="plan_retired",
        target_id=str(plan.id),
        detail=plan.slug,
        ip=client_ip(request),
    ).insert()
    return _plan_out(plan)


# --------------------------------------------------------------------------
# Leads — subscription requests coming off the public page.
# --------------------------------------------------------------------------


class LeadUpdate(BaseModel):
    status: LeadStatus


class ConvertLead(BaseModel):
    email: EmailStr
    display_name: str = Field(default="", max_length=120)
    organisation: str = Field(default="", max_length=160)
    monthly_quota: int = Field(default=500, ge=0, le=1_000_000)


def _lead_out(lead: Lead) -> dict:
    return {
        "id": str(lead.id),
        "full_name": lead.full_name,
        "whatsapp": lead.whatsapp,
        "plan_slug": lead.plan_slug,
        "plan_name": lead.plan_name,
        "note": lead.note,
        "status": lead.status,
        "created_at": lead.created_at,
        "contacted_at": lead.contacted_at,
    }


async def _lead_or_404(lead_id: str) -> Lead:
    lead = await Lead.get(lead_id)
    if lead is None:
        raise not_found("lead_not_found", "request not found")
    return lead


@router.get("/leads", response_model=list[dict])
async def list_leads(
    status_filter: LeadStatus | None = None,
    limit: int = 200,
    admin: User = Depends(current_admin),
) -> list[dict]:
    query = Lead.find(Lead.status == status_filter) if status_filter else Lead.find_all()
    leads = await query.sort(-Lead.created_at).limit(min(limit, 500)).to_list()
    return [_lead_out(lead) for lead in leads]


@router.patch("/leads/{lead_id}", response_model=dict)
async def update_lead(
    lead_id: str,
    payload: LeadUpdate,
    request: Request,
    admin: User = Depends(current_admin),
) -> dict:
    lead = await _lead_or_404(lead_id)
    lead.status = payload.status
    if payload.status == LeadStatus.CONTACTED and lead.contacted_at is None:
        lead.contacted_at = datetime.now(timezone.utc)
    await lead.save()
    await AuditLog(
        actor_id=str(admin.id),
        actor_email=admin.email,
        action="lead_updated",
        target_id=str(lead.id),
        detail=f"status={payload.status.value}",
        ip=client_ip(request),
    ).insert()
    return _lead_out(lead)


@router.post("/leads/{lead_id}/convert", response_model=CustomerCreated,
             status_code=status.HTTP_201_CREATED)
async def convert_lead(
    lead_id: str,
    payload: ConvertLead,
    request: Request,
    admin: User = Depends(current_admin),
) -> CustomerCreated:
    """Turn an answered request into a customer account.

    The email is asked for here rather than on the public form: the visitor gave
    us a WhatsApp number, and the account address is settled in the conversation
    that follows, not by whatever a stranger typed into a public field.
    """
    lead = await _lead_or_404(lead_id)
    if lead.status == LeadStatus.CONVERTED:
        raise AppError(status.HTTP_409_CONFLICT, "lead_converted",
                       "this request was already converted")

    customer, password = await _provision_customer(
        admin,
        request,
        email=payload.email,
        display_name=payload.display_name or lead.full_name,
        organisation=payload.organisation,
        monthly_quota=payload.monthly_quota,
    )
    lead.status = LeadStatus.CONVERTED
    if lead.contacted_at is None:
        lead.contacted_at = datetime.now(timezone.utc)
    await lead.save()
    await AuditLog(
        actor_id=str(admin.id),
        actor_email=admin.email,
        action="lead_converted",
        target_id=str(lead.id),
        detail=str(customer.id),
        ip=client_ip(request),
    ).insert()
    return CustomerCreated(**_out(customer).model_dump(), password=password)
