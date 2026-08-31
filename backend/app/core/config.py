"""Settings, read from the environment. Nothing here has a secret for a default.

A missing secret must stop the process at boot, not quietly fall back to a value
that happens to work on the developer's machine and is public knowledge in the
repository.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


def _load_env_file() -> None:
    """Read .env into the environment, without overriding what is already set.

    Real deployments pass configuration as environment variables; .env is a
    developer convenience. Values already present therefore win, so running with
    an explicit variable never silently picks up a stale file instead.
    """
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name}. "
            "Copy .env.example to .env and fill it in."
        )
    return value


def _flag(name: str, default: bool = False) -> bool:
    return (os.environ.get(name) or str(default)).strip().casefold() in {"1", "true", "yes", "on"}


class Settings:
    """Runtime configuration for the Excel Clear API."""

    def __init__(self) -> None:
        # --- database -----------------------------------------------------
        self.mongo_url: str = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
        self.mongo_db: str = os.environ.get("MONGO_DB", "excelclear")

        # --- auth ---------------------------------------------------------
        self.jwt_secret: str = _require("JWT_SECRET")
        self.jwt_algorithm: str = "HS256"
        self.access_token_minutes: int = int(os.environ.get("ACCESS_TOKEN_MINUTES", "30"))
        self.refresh_token_days: int = int(os.environ.get("REFRESH_TOKEN_DAYS", "14"))
        # Five failures then a lockout: enough for a fat-fingered password,
        # far too few for a dictionary run.
        self.max_login_attempts: int = int(os.environ.get("MAX_LOGIN_ATTEMPTS", "5"))
        self.lockout_minutes: int = int(os.environ.get("LOCKOUT_MINUTES", "15"))
        # Seeded once, on an empty database. Not ".local": that suffix is
        # reserved and address validation rejects it.
        self.first_admin_email: str = os.environ.get(
            "FIRST_ADMIN_EMAIL", "admin@excelclear.app"
        ).strip().lower()

        # --- storage ------------------------------------------------------
        self.storage_path: Path = Path(os.environ.get("STORAGE_PATH", "./storage")).resolve()
        self.max_upload_mb: int = int(os.environ.get("MAX_UPLOAD_MB", "25"))
        self.max_pdf_pages: int = int(os.environ.get("MAX_PDF_PAGES", "20"))
        self.max_image_pixels: int = int(os.environ.get("MAX_IMAGE_PIXELS", str(80_000_000)))

        # --- AI provider --------------------------------------------------
        # "local"      runs the bundled vl_worker on this machine
        # "http"       calls a self-hosted GPU service (Modal, RunPod, …)
        # "openrouter" calls a hosted vision model
        self.ai_provider: str = os.environ.get("AI_PROVIDER", "local").strip().casefold()
        self.openrouter_key: str = os.environ.get("OPENROUTER_API_KEY", "").strip()
        self.openrouter_model: str = os.environ.get(
            "OPENROUTER_MODEL", "qwen/qwen2.5-vl-72b-instruct"
        ).strip()
        # Sent as HTTP-Referer for usage attribution. Not a credential.
        self.openrouter_site: str = os.environ.get("OPENROUTER_SITE", "").strip()
        # Tried in order when the preferred model is busy or out of credit. A
        # second hosted model answers in seconds; the local reader takes
        # minutes, so it is worth asking a couple before dropping off the
        # network entirely.
        self.openrouter_alternates: tuple[str, ...] = tuple(
            m.strip() for m in os.environ.get("OPENROUTER_FALLBACK_MODELS", "").split(",")
            if m.strip()
        )
        # Room for a whole page of invoice transcribed as HTML. Too low and the
        # answer stops mid-table with no error, which reads as a page that ended
        # early rather than one that was cut off.
        self.openrouter_max_tokens: int = max(
            1000, int(os.environ.get("OPENROUTER_MAX_TOKENS", "24000") or 24000)
        )
        self.ai_service_url: str = os.environ.get("AI_SERVICE_URL", "").strip()
        # The shared secret this backend presents to the GPU service. Named for
        # what it is, not for whoever happens to host the service today —
        # reading it from HF_TOKEN was a leftover that sent an empty header and
        # earned a 401 from a service that was working perfectly.
        self.ai_service_token: str = (
            os.environ.get("AI_SERVICE_KEY") or os.environ.get("HF_TOKEN") or ""
        ).strip()
        self.ai_timeout_seconds: float = float(os.environ.get("AI_TIMEOUT_SECONDS", "900"))
        # When the GPU service is unreachable, fall back to the local reader
        # rather than failing the job outright.
        self.ai_fallback_local: bool = _flag("AI_FALLBACK_LOCAL", True)

        # --- worker -------------------------------------------------------
        # The reading pipeline. This is the desktop product's code, reused
        # verbatim rather than reimplemented; a copy ships in this repository so
        # a fresh clone can process a document without hunting for it. Point
        # WORKER_ROOT at the desktop tree instead when working on both together.
        self.worker_root: Path = Path(
            os.environ.get("WORKER_ROOT", "../ocr_worker")
        ).resolve()
        self.worker_python: str = os.environ.get("WORKER_PYTHON", "").strip()
        self.job_concurrency: int = int(os.environ.get("JOB_CONCURRENCY", "1"))

        # --- api ----------------------------------------------------------
        self.cors_origins: list[str] = [
            origin.strip()
            for origin in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")
            if origin.strip()
        ]
        self.debug: bool = _flag("DEBUG", False)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
