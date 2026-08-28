"""Running one job.

The extraction logic is *not* reimplemented here. It is the same code the
desktop product uses — ``perceive`` for independent evidence, ``ai_extract`` for
the three gates, ``excel_builder`` for the workbook — and this module only
arranges it into a background job, in its own process, recording how long each
stage took.

The division of labour is deliberate:

* the vision model runs wherever :mod:`ai_provider` says (a GPU, or locally);
* **verification always runs on our side**, against evidence from a different
  reader. A model that graded its own output would prove nothing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.models.entities import FlaggedValue, Job, JobStatus, Stage, StageTiming, User
from app.services.runner import PROGRESS as PROGRESS_MARKER
from app.services.storage import result_path

logger = logging.getLogger("excelclear.pipeline")


def _record_stage(job: Job, stage: Stage, ms: int, detail: str = "") -> None:
    """Mark a stage finished, in place.

    The reader reports a running total per stage rather than a per-page figure,
    because a multi-page document passes through each stage several times and
    the reader is the only side that knows how many times that was. So an
    existing entry is updated, never appended to.
    """
    finished = datetime.now(timezone.utc)
    for entry in job.stages:
        if entry.stage == stage:
            entry.ms = ms
            entry.finished_at = finished
            if detail:
                entry.detail = detail
            return
    job.stages.append(StageTiming(stage=stage, ms=ms, detail=detail, finished_at=finished))


def _pump(stream, kind: str, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
    """Read one child stream on a worker thread, handing each line to the loop.

    A thread rather than ``asyncio.create_subprocess_exec``, deliberately. That
    call raises ``NotImplementedError`` on Windows whenever the server happens to
    be running on a selector event loop — which is what uvicorn's ``--reload``
    gives you — and the failure arrives as an exception with an empty message,
    so the customer is told only that their document could not be read. Reading
    the pipe on a thread works on every loop and platform, and takes the whole
    question out of the most important path in the product.
    """
    try:
        for raw in stream:
            loop.call_soon_threadsafe(queue.put_nowait, (kind, raw.rstrip("\r\n")))
    except Exception:  # a closed pipe is the normal end of a crashed child
        pass
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, (kind + ":eof", ""))


async def _run_in_subprocess(payload: dict[str, Any], on_progress) -> dict[str, Any]:
    """Hand the work to :mod:`app.services.runner` and follow it as it goes.

    A child process, not a thread: importing Paddle inside the server killed it
    outright — a native crash with no Python traceback, taking every in-flight
    request with it. Out here, the worst a bad page can do is fail its own job.

    Its stdout is read as it arrives rather than collected at the end, because
    that stream is how the reader says which stage it is on. Waiting for the
    process to exit before reading it would give us the whole story only once
    there was nobody left waiting to hear it.
    """
    import tempfile

    settings = get_settings()
    with tempfile.TemporaryDirectory(prefix="ec-job-") as workspace:
        request_path = Path(workspace) / "request.json"
        result_json = Path(workspace) / "result.json"
        request = {
            **payload,
            "result_json": str(result_json),
            "worker_root": str(settings.worker_root),
        }
        request_path.write_text(json.dumps(request), encoding="utf-8")

        # No console window per job on Windows: a flash of black for every page
        # is not something to inflict on someone processing a batch.
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "app.services.runner", str(request_path)],
            cwd=str(Path(__file__).resolve().parents[2]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=no_window,
        )

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        for stream, kind in ((process.stdout, "out"), (process.stderr, "err")):
            threading.Thread(
                target=_pump, args=(stream, kind, loop, queue), daemon=True
            ).start()

        errors: list[str] = []
        open_streams = 2
        while open_streams:
            kind, line = await queue.get()
            if kind.endswith(":eof"):
                open_streams -= 1
            elif kind == "out" and line.startswith(PROGRESS_MARKER):
                try:
                    await on_progress(json.loads(line[len(PROGRESS_MARKER):]))
                except Exception:
                    logger.debug("unreadable progress line: %r", line[:200])
            elif kind == "err" and line:
                errors.append(line)
                del errors[:-20]

        # The wait is off the loop as well: the child has closed its pipes by
        # now, but joining it must never block every other request.
        returncode = await asyncio.to_thread(process.wait)

        if result_json.is_file():
            return json.loads(result_json.read_text(encoding="utf-8"))
        # No result file means the child died before writing one — the native
        # crash case. Surface the tail of its stderr rather than a bare exit
        # code, which explains nothing to anyone reading the job later.
        return {
            "ok": False,
            "code": "reader_crashed",
            "error": f"the reader stopped unexpectedly (exit {returncode})"
                     + (f": {errors[-1]}" if errors else ""),
        }


async def run_job(job_id: str) -> None:
    """Process one job end to end, recording every stage as it happens."""
    job = await Job.get(job_id)
    if job is None or job.status != JobStatus.QUEUED:
        return

    started_at = datetime.now(timezone.utc)
    job.status = JobStatus.PROCESSING
    job.started_at = started_at
    job.stage = Stage.EVIDENCE_OCR
    job.stage_started_at = started_at
    job.pages_done = 0
    # The upload is finished the moment the job exists — the file is already on
    # disk. Recording it here is what lets the interface tick it off straight
    # away instead of leaving a completed step looking pending for the whole run.
    job.stages = [StageTiming(stage=Stage.UPLOAD, ms=0, finished_at=started_at)]
    await job.save()

    settings = get_settings()
    payload = {
        "source": str(settings.storage_path / "uploads" / job.customer_id / job.stored_name),
        "result_dir": str(result_path(job.customer_id, str(job.id))),
        "pages": job.page_count,
    }

    async def on_progress(event: dict[str, Any]) -> None:
        """Record one step of the reader's own account of itself."""
        try:
            stage = Stage(event.get("stage", ""))
        except ValueError:
            return
        page = int(event.get("page") or 0)

        if event.get("state") == "running":
            job.stage = stage
            job.stage_started_at = datetime.now(timezone.utc)
        else:
            _record_stage(job, stage, int(event.get("ms") or 0))
            if event.get("page_complete"):
                job.pages_done = max(job.pages_done, page)
        await job.save()

    try:
        outcome = await _run_in_subprocess(payload, on_progress)
    except Exception as error:
        logger.exception("job %s failed", job_id)
        # Some exceptions carry no message at all — NotImplementedError is one —
        # and recording an empty string leaves the customer with a generic
        # sentence and nothing to pass on to anyone who could help.
        await _fail(job, "reader_failed", str(error) or type(error).__name__)
        return
    if not outcome.get("ok"):
        await _fail(
            job,
            str(outcome.get("code") or "reader_failed"),
            str(outcome.get("error") or "processing failed"),
        )
        return

    # The result file is authoritative for the timings — the progress lines were
    # a live estimate, these are what the reader actually measured.
    timings = outcome["timings"]
    _record_stage(job, Stage.EVIDENCE_OCR, timings.get("evidence_ocr", 0))
    _record_stage(job, Stage.AI_VISION, timings.get("ai_vision", 0),
                  detail=f"queue {timings.get('ai_queue', 0)}ms")
    _record_stage(job, Stage.VERIFICATION, timings.get("verification", 0))
    _record_stage(job, Stage.EXCEL, timings.get("excel", 0))
    job.status = JobStatus.COMPLETED
    job.stage = None
    job.stage_started_at = None
    job.pages_done = outcome["pages"]
    job.finished_at = datetime.now(timezone.utc)
    job.result_path = outcome["result"]
    job.items_extracted = outcome["records"]
    job.page_count = outcome["pages"]
    job.warnings = outcome["warnings"]
    job.ai_provider = outcome["provider"]
    job.ai_model = outcome["model"]
    job.flagged = [FlaggedValue(**item) for item in outcome["flagged"]]
    await job.save()

    # Quota is spent on work actually delivered, not on attempts.
    customer = await User.get(job.customer_id)
    if customer is not None:
        period = datetime.now(timezone.utc).strftime("%Y-%m")
        if customer.quota_period != period:
            customer.quota_period = period
            customer.used_this_month = 0
        customer.used_this_month += 1
        await customer.save()

    logger.info(
        "JOB_DONE id=%s provider=%s pages=%d items=%d flagged=%d total_ms=%d",
        job_id, job.ai_provider, job.page_count, job.items_extracted,
        job.flagged_count, job.total_ms,
    )


async def _fail(job: Job, code: str, message: str) -> None:
    """Stop the job, keeping both a code the interface can translate and the
    original text, which is the only thing that helps whoever debugs it."""
    job.status = JobStatus.FAILED
    # The stage it died on is deliberately left set: "failed during the vision
    # model" is a different fact from "failed", and the tracker shows which.
    job.stage_started_at = None
    job.error = message
    job.error_code = code
    job.finished_at = datetime.now(timezone.utc)
    await job.save()
    logger.warning("JOB_FAILED id=%s code=%s %s", job.id, code, message)
