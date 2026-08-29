"""Upload a document, watch it process, download the workbook."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.api.deps import client_ip, current_user, owned_job
from app.core.config import get_settings
from app.core.errors import AppError, not_found
from app.models.entities import AuditLog, Job, JobKind, JobStatus, Role, User, as_utc
from app.services.pipeline import run_job
from app.services.storage import UploadRejected, count_pdf_pages, store_upload

logger = logging.getLogger("excelclear.jobs")
router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# One page at a time per process. The vision model cannot be called
# concurrently, and pretending otherwise would corrupt results rather than
# speed anything up.
_slots = asyncio.Semaphore(max(1, get_settings().job_concurrency))


class StageOut(BaseModel):
    stage: str
    ms: int
    detail: str = ""


class FlagOut(BaseModel):
    cell: str
    value: object = None
    reason: str
    gate: str


class JobOut(BaseModel):
    id: str
    filename: str
    kind: JobKind = JobKind.EXTRACT
    status: JobStatus
    stage: str | None = None
    items: int = 0
    flagged: int = 0
    pages: int = 0
    pages_done: int = 0
    provider: str = ""
    total_ms: int = 0
    # Two clocks, both measured on the server at the moment of the reply, so
    # neither depends on the customer's device agreeing with us about the time.
    # The browser counts on from them locally between polls.
    #
    # `elapsed_ms` is wall time since the work started and only ever grows.
    # `stage_ms` is how long the current stage has been going, and resets each
    # time one begins — which is why the two cannot be the same number, and why
    # the header must not try to add stage timings up to reach the first.
    elapsed_ms: int = 0
    stage_ms: int = 0
    stages: list[StageOut] = []
    error: str | None = None
    error_code: str = ""
    created_at: datetime


class JobDetail(JobOut):
    flags: list[FlagOut] = []
    warnings: list[str] = []
    has_result: bool = False


def _out(job: Job) -> JobOut:
    now = datetime.now(timezone.utc)
    stage_started = as_utc(job.stage_started_at)
    stage_ms = 0
    if job.status == JobStatus.PROCESSING and stage_started is not None:
        stage_ms = max(0, int((now - stage_started).total_seconds() * 1000))

    began, ended = as_utc(job.started_at), as_utc(job.finished_at)
    elapsed_ms = 0
    if began is not None:
        until = now if job.status == JobStatus.PROCESSING else (ended or now)
        elapsed_ms = max(0, int((until - began).total_seconds() * 1000))
    return JobOut(
        id=str(job.id),
        filename=job.filename,
        kind=job.kind,
        status=job.status,
        stage=job.stage.value if job.stage else None,
        items=job.items_extracted,
        flagged=job.flagged_count,
        pages=job.page_count,
        pages_done=job.pages_done,
        provider=job.ai_provider,
        total_ms=job.total_ms,
        elapsed_ms=elapsed_ms,
        stage_ms=stage_ms,
        stages=[StageOut(stage=s.stage.value, ms=s.ms, detail=s.detail) for s in job.stages],
        error=job.error,
        error_code=job.error_code,
        created_at=job.created_at,
    )


async def _process(job_id: str) -> None:
    async with _slots:
        await run_job(job_id)


@router.post("", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    request: Request,
    file: UploadFile = File(...),
    kind: JobKind = Form(JobKind.EXTRACT),
    user: User = Depends(current_user),
) -> JobOut:
    """Accept a document and start work. Returns immediately with a job id.

    The HTTP request does not wait for the reading: a page can take minutes, and
    a connection held open that long fails for reasons that have nothing to do
    with the document.

    ``kind`` decides which work is asked for, and it changes what the upload is
    allowed to be: ``extract`` reads a page and spends quota, ``clean`` tidies a
    spreadsheet the customer already has and spends none.
    """
    if user.role != Role.CUSTOMER:
        raise AppError(
            status.HTTP_403_FORBIDDEN, "customer_only", "sign in as a customer to upload"
        )
    cleaning = kind == JobKind.CLEAN
    # A plan that has been assigned but not switched on. Only ever a gate for an
    # account that is actually on a plan: an account with none is on no plan to
    # activate, and every account created before plans existed is one of those.
    if not cleaning and user.plan_slug and not user.plan_active:
        raise AppError(
            status.HTTP_403_FORBIDDEN,
            "plan_inactive",
            "Your plan has not been activated yet. Ask your administrator to activate it.",
        )
    # No quota gate on cleaning: nothing about it costs per document, so a limit
    # on it would be a charge for nothing.
    if not cleaning and user.monthly_quota and user.quota_remaining <= 0:
        raise AppError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "quota_exhausted",
            "You have used this month's quota. Ask your administrator to raise it.",
            quota=user.monthly_quota,
        )

    data = await file.read()
    try:
        stored = store_upload(
            data, file.filename or "document", str(user.id), spreadsheet=cleaning
        )
    except UploadRejected as error:
        raise AppError(
            status.HTTP_400_BAD_REQUEST, error.code, str(error), **error.params
        ) from error

    settings = get_settings()
    pages = 1 if cleaning else count_pdf_pages(stored.path)
    if pages == 0:
        stored.path.unlink(missing_ok=True)
        raise AppError(
            status.HTTP_400_BAD_REQUEST, "pdf_unreadable", "This PDF could not be opened."
        )
    if pages > settings.max_pdf_pages:
        stored.path.unlink(missing_ok=True)
        raise AppError(
            status.HTTP_400_BAD_REQUEST,
            "too_many_pages",
            f"This document has {pages} pages; the limit is {settings.max_pdf_pages}.",
            pages=pages,
            limit=settings.max_pdf_pages,
        )

    job = Job(
        customer_id=str(user.id),
        admin_id=user.admin_id,
        kind=kind,
        filename=stored.original_name,
        stored_name=stored.stored_name,
        content_type=stored.content_type,
        size_bytes=stored.size_bytes,
        page_count=pages,
    )
    await job.insert()
    await AuditLog(
        actor_id=str(user.id),
        actor_email=user.email,
        action="job_created",
        target_id=str(job.id),
        detail=stored.original_name,
        ip=client_ip(request),
    ).insert()

    asyncio.create_task(_process(str(job.id)))
    return _out(job)


@router.get("", response_model=list[JobOut])
async def list_jobs(limit: int = 50, user: User = Depends(current_user)) -> list[JobOut]:
    jobs = (
        await Job.find(Job.customer_id == str(user.id))
        .sort(-Job.created_at)
        .limit(min(limit, 200))
        .to_list()
    )
    return [_out(job) for job in jobs]


@router.get("/{job_id}", response_model=JobDetail)
async def job_detail(job: Job = Depends(owned_job)) -> JobDetail:
    return JobDetail(
        **_out(job).model_dump(),
        flags=[FlagOut(**flag.model_dump()) for flag in job.flagged],
        warnings=job.warnings,
        has_result=bool(job.result_path and Path(job.result_path).is_file()),
    )


@router.get("/{job_id}/result")
async def download(job: Job = Depends(owned_job)) -> FileResponse:
    if not job.result_path or not Path(job.result_path).is_file():
        raise not_found("no_workbook", "no workbook for this job yet")
    return FileResponse(
        job.result_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{Path(job.filename).stem}.xlsx",
    )


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel(job: Job = Depends(owned_job)) -> JobOut:
    if job.status in {JobStatus.COMPLETED, JobStatus.FAILED}:
        raise AppError(
            status.HTTP_409_CONFLICT, "job_finished", "this job has already finished"
        )
    job.status = JobStatus.CANCELLED
    job.finished_at = datetime.now(timezone.utc)
    await job.save()
    return _out(job)
