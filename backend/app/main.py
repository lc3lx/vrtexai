"""Excel Clear API."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import admin, auth, jobs, public
from app.core.config import get_settings
from app.core.db import connect, disconnect
from app.core.errors import validation_handler

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s  %(message)s"
)
logger = logging.getLogger("excelclear")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    try:
        import truststore

        # Lets outbound calls to the GPU service verify against the OS trust
        # store, which is what makes them work behind an HTTPS-inspecting proxy.
        truststore.inject_into_ssl()
    except Exception as error:
        logger.warning("OS trust store unavailable: %s", error)

    settings.storage_path.mkdir(parents=True, exist_ok=True)
    await connect()
    logger.info(
        "ready · ai_provider=%s · fallback=%s · storage=%s",
        settings.ai_provider, settings.ai_fallback_local, settings.storage_path,
    )
    _warn_if_the_safety_net_is_missing(settings)
    yield
    await disconnect()


def _warn_if_the_safety_net_is_missing(settings) -> None:
    """Say at boot when ``AI_FALLBACK_LOCAL`` promises something that is not there.

    The local reader ships with the desktop build; a server usually has neither
    the environment nor the model weights. Left unsaid, the shortfall surfaces
    only much later, as a job that fails the first time the hosted model is
    busy — and with the local reader's complaint on it, which is the wrong thing
    to go and fix.
    """
    if not settings.ai_fallback_local or settings.ai_provider == "local":
        return
    try:
        from app.services.ai_provider import LocalProvider

        ready, detail = LocalProvider(settings.worker_root).health()
    except Exception as error:  # a broken check must not stop the service
        ready, detail = False, f"{type(error).__name__}: {error}"
    if not ready:
        logger.warning(
            "AI_FALLBACK_LOCAL is on but the local reader cannot run here (%s). "
            "When %s is busy or out of credit, jobs will fail rather than fall back.",
            detail, settings.ai_provider,
        )


app = FastAPI(
    title="Excel Clear",
    version="1.0.0",
    description="Verified document extraction to Excel.",
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(jobs.router)
app.include_router(public.router)


# Field-level 422s become one coded message the browser can translate.
app.add_exception_handler(RequestValidationError, validation_handler)


@app.exception_handler(Exception)
async def unhandled(request: Request, error: Exception) -> JSONResponse:
    # Never return a stack trace to a browser: it names internal paths and
    # library versions, which is free reconnaissance.
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        {"detail": {
            "code": "server_error",
            "message": "Something went wrong on our side. The error has been logged.",
            "params": {},
        }},
        status_code=500,
    )


@app.get("/api/health")
async def health() -> dict:
    from app.services.ai_provider import build_provider

    ready, detail = build_provider(get_settings()).health()
    return {"ok": True, "ai_ready": ready, "ai_detail": detail}


# The browser client is plain files served by this same process: one thing to
# run, one origin, and no build step between editing and seeing the change.
_frontend = Path(__file__).resolve().parents[2] / "frontend"
if _frontend.is_dir():
    app.mount("/static", StaticFiles(directory=_frontend), name="static")

    @app.get("/", include_in_schema=False)
    async def landing() -> FileResponse:
        """The public page. The sign-in gate moved to /app when this arrived."""
        return FileResponse(_frontend / "landing.html")

    @app.get("/app", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(_frontend / "index.html")
