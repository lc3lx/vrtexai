"""Where the vision model runs — and nothing else.

The desktop application already separates reading a page from judging what was
read: :mod:`vl_worker` runs PaddleOCR-VL in its own process and answers with one
JSON object per page. This module turns that seam into a swappable one, so the
model can move to a GPU service without any other part of the system knowing.

Two rules shape the design:

* **The schema is the existing one.** ``vl_worker`` already emits
  ``{"result": ..., "markdown": ...}`` per page and ``paddle_vl.to_payload``
  already consumes it. Inventing a second shape for the HTTP hop would mean two
  parsers to keep in agreement, so the wire format *is* the worker's format.
* **The provider never judges.** It returns page structure. The three gates in
  ``ai_extract`` — shape, arithmetic, and pixel evidence from an independent
  reader — stay on this side of the wire. A model that graded its own homework
  would make the whole product worthless.
"""
from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("excelclear.ai")


def _trust_the_operating_system() -> None:
    """Verify TLS against the OS certificate store, in every process.

    Networks that inspect HTTPS re-sign traffic with a local authority that
    Windows trusts but that certifi — the CA list the HTTP libraries ship — has
    never heard of. This lives here, at import, rather than in the web app's
    start-up, because the job runner is a *separate process*: doing it once in
    the server left the child verifying against certifi, and a working GPU
    service came back as "unreachable, falling back to local".
    """
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception as error:  # a plain network needs no help
        logger.debug("OS trust store unavailable: %s", error)


_trust_the_operating_system()


class AIUnavailable(RuntimeError):
    """The provider could not be reached. Callers may fall back."""


class AIFailed(RuntimeError):
    """The provider was reached and refused, or answered nonsense."""


@dataclass
class PageStructure:
    """One page as the model saw it, in the worker's own vocabulary."""

    result: dict[str, Any] = field(default_factory=dict)
    markdown: str = ""

    def as_payload(self) -> dict[str, Any]:
        """The shape ``paddle_vl.to_payload`` expects."""
        return {"result": self.result, "markdown": self.markdown}


@dataclass
class ReadOutcome:
    pages: list[PageStructure]
    provider: str
    inference_ms: int
    queue_ms: int = 0
    model: str = "PaddleOCR-VL-0.9B"
    # Set only when the GPU service was skipped, so the job records why.
    fallback_reason: str = ""


class AIProvider(abc.ABC):
    """Reads a page image and returns its structure. Nothing more."""

    name: str = "abstract"

    @abc.abstractmethod
    def read(self, image_path: Path) -> ReadOutcome:
        """Structure for one page image. Raises AIUnavailable or AIFailed."""

    def health(self) -> tuple[bool, str]:
        return True, self.name

    def close(self) -> None:
        """Release anything long-lived. Safe to call twice."""


# ---------------------------------------------------------------------------
# Local — the desktop path, unchanged
# ---------------------------------------------------------------------------
class LocalProvider(AIProvider):
    """Runs the bundled ``vl_worker`` exactly as the desktop application does.

    This is the fallback that has to keep working when there is no GPU service
    and no internet, so it deliberately calls the existing module rather than a
    copy of it.
    """

    name = "local"

    def __init__(self, worker_root: Path) -> None:
        self._root = Path(worker_root)

    def _paddle_vl(self):
        import sys

        root = str(self._root)
        if root not in sys.path:
            sys.path.insert(0, root)
        import paddle_vl  # noqa: PLC0415 — imported late so the web app boots without it

        return paddle_vl

    def health(self) -> tuple[bool, str]:
        try:
            return self._paddle_vl().available()
        except Exception as error:
            return False, f"{type(error).__name__}: {error}"

    def read(self, image_path: Path) -> ReadOutcome:
        try:
            paddle_vl = self._paddle_vl()
        except Exception as error:
            raise AIUnavailable(f"local vision worker unavailable: {error}") from error

        started = time.perf_counter()
        try:
            raw = paddle_vl._run_worker(Path(image_path))
        except Exception as error:
            raise AIFailed(str(error)) from error
        elapsed = int((time.perf_counter() - started) * 1000)

        pages = [
            PageStructure(result=page.get("result") or {}, markdown=str(page.get("markdown") or ""))
            for page in (raw.get("pages") or [])
        ]
        if not pages:
            raise AIFailed("the local vision worker returned no pages")
        logger.info("INFERENCE_END provider=local ms=%d pages=%d", elapsed, len(pages))
        return ReadOutcome(pages=pages, provider=self.name, inference_ms=elapsed)

    def close(self) -> None:
        try:
            self._paddle_vl().shutdown()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# HTTP — a GPU service somewhere else
# ---------------------------------------------------------------------------
class HttpProvider(AIProvider):
    """Posts the page to a GPU service that speaks the worker's JSON.

    Deliberately ignorant of who is hosting it. Hugging Face today, a rented GPU
    tomorrow — swapping hosts is a URL and a token, not a code change.
    """

    name = "http"

    def __init__(self, url: str, token: str = "", timeout: float = 900.0) -> None:
        if not url:
            raise ValueError("AI_SERVICE_URL is empty but AI_PROVIDER is 'http'")
        self._url = url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        # The token lives in the backend's environment and never leaves it: the
        # browser talks to this API, this API talks to the GPU service.
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    # A scale-to-zero service is asleep between bursts, and waking it means
    # starting a container and loading the model. Long enough to let that
    # finish, short enough that a genuinely dead service is not waited on.
    HEALTH_TIMEOUT = 90.0

    def health(self) -> tuple[bool, str]:
        import httpx

        try:
            response = httpx.get(
                f"{self._url}/health",
                headers=self._headers(),
                timeout=self.HEALTH_TIMEOUT,
                follow_redirects=True,
            )
            response.raise_for_status()
            body = response.json()
            return bool(body.get("ready")), str(body.get("detail") or self._url)
        except httpx.TimeoutException:
            # Asleep is not broken. Saying "unavailable" here would send an
            # administrator hunting for an outage that does not exist, and a
            # real job would wake the container and succeed anyway.
            return True, "waking up (scaled to zero)"
        except Exception as error:
            return False, f"{type(error).__name__}: {error}"

    def read(self, image_path: Path) -> ReadOutcome:
        import httpx

        started = time.perf_counter()
        logger.info("INFERENCE_START provider=http url=%s", self._url)
        try:
            with open(image_path, "rb") as handle:
                response = httpx.post(
                    f"{self._url}/process",
                    files={"file": (Path(image_path).name, handle, "application/octet-stream")},
                    headers=self._headers(),
                    timeout=self._timeout,
                    # Modal answers a long-running request with 303 to a result
                    # URL rather than holding the connection open. Without
                    # following it, a perfectly good reading arrives as an
                    # unhandled redirect and the job fails for no reason.
                    follow_redirects=True,
                )
        except httpx.TimeoutException as error:
            raise AIUnavailable(f"vision service timed out after {self._timeout:.0f}s") from error
        except httpx.HTTPError as error:
            raise AIUnavailable(f"vision service unreachable: {error}") from error

        # 5xx and 429 mean "try elsewhere or later"; 4xx means this request is
        # wrong and retrying it changes nothing.
        if response.status_code >= 500 or response.status_code == 429:
            raise AIUnavailable(f"vision service returned {response.status_code}")
        if response.status_code >= 400:
            raise AIFailed(f"vision service rejected the request ({response.status_code})")

        try:
            body = response.json()
        except ValueError as error:
            raise AIFailed("vision service did not return JSON") from error
        if not body.get("success", True):
            raise AIFailed(str(body.get("error") or "vision service reported a failure"))

        pages = [
            PageStructure(result=page.get("result") or {}, markdown=str(page.get("markdown") or ""))
            for page in (body.get("pages") or [])
            if isinstance(page, dict)
        ]
        if not pages:
            raise AIFailed("vision service returned no pages")

        elapsed = int((time.perf_counter() - started) * 1000)
        logger.info("INFERENCE_END provider=http ms=%d pages=%d", elapsed, len(pages))
        return ReadOutcome(
            pages=pages,
            provider=self.name,
            inference_ms=int(body.get("inference_ms") or elapsed),
            queue_ms=int(body.get("queue_ms") or 0),
            model=str(body.get("model") or "PaddleOCR-VL-0.9B"),
        )


# ---------------------------------------------------------------------------
# OpenRouter — a hosted vision model, adapted to the worker's schema
# ---------------------------------------------------------------------------
READ_PAGE_PROMPT = """Transcribe this document page exactly as printed.

Rules:
- Every table becomes an HTML <table> with <tr> and <td>. Keep the original
  column order and one <td> per printed cell, including empty ones.
- Every <tr> in a table must have the SAME number of <td> as the header row.
  Never use colspan or rowspan. Where the page merges cells, write the text in
  the first column it covers and write <td></td> for each remaining column.
- A total, tax or balance line printed inside a table stays a row of that same
  table, with its label in the first column and its amount under the amounts.
- Numbers exactly as printed: keep separators and decimals, drop currency signs.
- Never compute, correct, infer or complete a value. If a cell is unreadable,
  write it as best you can and move on.
- Text outside tables goes in <p> tags, in reading order.
- Output HTML only. No commentary, no markdown fences."""


def _classify_openrouter_error(error: Any) -> Exception:
    """Decide whether an error body is worth falling back over.

    OpenRouter often answers **HTTP 200** and puts the upstream provider's
    failure inside the body — a rate limit on a free model arrives as
    ``{"error": {"code": 429}}`` with a 200 status. Reading only the HTTP status
    therefore classifies a passing condition as a permanent one, and the local
    reader that exists precisely for this never gets its turn. The code inside
    the body is the one that matters.
    """
    detail = error if isinstance(error, dict) else {"message": str(error)}
    message = str(detail.get("message") or detail)
    try:
        code = int(detail.get("code") or 0)
    except (TypeError, ValueError):
        code = 0
    metadata = detail.get("metadata") or {}
    if isinstance(metadata, dict) and metadata.get("raw"):
        message = f"{message}: {str(metadata['raw'])[:160]}"

    # 402 out of credit, 408/429 busy, 5xx upstream broken — all of them pass,
    # and all of them mean "read this page somewhere else for now".
    if code in (402, 408, 429) or code >= 500:
        return AIUnavailable(f"OpenRouter upstream {code}: {message}")
    return AIFailed(f"OpenRouter error {code or '?'}: {message}")


class OpenRouterProvider(AIProvider):
    """Reads a page with a hosted vision model and rebuilds the worker's schema.

    The point of this class is the adapter at the bottom. A chat model answers
    with prose or HTML, while everything downstream — ``paddle_vl.to_payload``,
    the role resolver, the three gates — expects PaddleOCR-VL's block list. So
    the HTML is turned back into ``parsing_res_list`` blocks and the rest of the
    system never learns that the reader changed.

    What does *not* change is the safety net. The model is asked to transcribe,
    never to compute: column roles are still resolved from whether the printed
    arithmetic holds, and every figure is still checked against word boxes from
    an independent local reader. A model that filled in a plausible total would
    be caught by the same gates that catch PaddleOCR-VL.
    """

    name = "openrouter"
    ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, model: str, timeout: float = 300.0,
                 site: str = "", title: str = "Excel Clear",
                 alternates: tuple[str, ...] = ()) -> None:
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is empty but AI_PROVIDER is 'openrouter'")
        self._key = api_key
        self._model = model
        self._timeout = timeout
        self._site = site
        self._title = title
        # Tried in order when the preferred model is busy or out of credit.
        # A second hosted model answers in seconds; the local reader takes
        # minutes, so it is worth asking two or three before giving up on the
        # network entirely.
        self._alternates = tuple(m for m in alternates if m and m != model)

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        # OpenRouter attributes usage to these; they are not credentials.
        if self._site:
            headers["HTTP-Referer"] = self._site
        headers["X-Title"] = self._title
        return headers

    def health(self) -> tuple[bool, str]:
        import httpx

        try:
            response = httpx.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {self._key}"}, timeout=20,
            )
            if response.status_code == 401:
                return False, "OpenRouter rejected the API key"
            response.raise_for_status()
            data = (response.json() or {}).get("data") or {}
            limit, usage = data.get("limit"), data.get("usage")
            budget = "unlimited" if limit is None else f"{usage or 0:.2f}/{limit}"
            return True, f"{self._model} · credit {budget}"
        except Exception as error:
            return False, f"{type(error).__name__}: {error}"

    @staticmethod
    def _data_url(image_path: Path) -> str:
        import base64
        import mimetypes

        raw = Path(image_path).read_bytes()
        mime = mimetypes.guess_type(str(image_path))[0] or "image/png"
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    def read(self, image_path: Path) -> ReadOutcome:
        """Read the page, trying each configured model until one answers."""
        attempts = (self._model, *self._alternates)
        last: Exception | None = None
        for model in attempts:
            try:
                return self._read_with(image_path, model)
            except AIUnavailable as error:
                # Busy or out of credit: another model may well be free.
                last = error
                logger.warning("model %s unavailable (%s), trying next", model, error)
            except AIFailed as error:
                # A rejected request is rejected everywhere; stop asking.
                raise error
        raise AIUnavailable(
            f"no OpenRouter model could read the page (tried {len(attempts)}): {last}"
        )

    def _read_with(self, image_path: Path, model: str) -> ReadOutcome:
        import httpx

        started = time.perf_counter()
        logger.info("INFERENCE_START provider=openrouter model=%s", model)
        payload = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": READ_PAGE_PROMPT},
                    {"type": "image_url", "image_url": {"url": self._data_url(image_path)}},
                ],
            }],
            # Transcription, not composition: sampling variety here would mean a
            # different reading of the same invoice on every attempt.
            "temperature": 0,
        }
        try:
            response = httpx.post(self.ENDPOINT, headers=self._headers(),
                                  json=payload, timeout=self._timeout)
        except httpx.TimeoutException as error:
            raise AIUnavailable(f"OpenRouter timed out after {self._timeout:.0f}s") from error
        except httpx.HTTPError as error:
            raise AIUnavailable(f"OpenRouter unreachable: {error}") from error

        # 402 is "out of credit" and 429 "slow down" — both are conditions that
        # pass, so the local reader should take over rather than the job failing.
        if response.status_code in (402, 429) or response.status_code >= 500:
            raise AIUnavailable(f"OpenRouter returned {response.status_code}: {response.text[:160]}")
        if response.status_code >= 400:
            raise AIFailed(f"OpenRouter rejected the request ({response.status_code}): "
                           f"{response.text[:200]}")

        body = response.json()
        if body.get("error"):
            raise _classify_openrouter_error(body["error"])
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise AIFailed("OpenRouter returned no message content") from error
        if isinstance(content, list):  # some models answer in parts
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not str(content).strip():
            raise AIFailed("the model returned an empty page")

        usage = body.get("usage") or {}
        elapsed = int((time.perf_counter() - started) * 1000)
        logger.info(
            "INFERENCE_END provider=openrouter model=%s ms=%d prompt_tokens=%s completion_tokens=%s",
            model, elapsed, usage.get("prompt_tokens"), usage.get("completion_tokens"),
        )
        return ReadOutcome(
            pages=[PageStructure(result=html_to_blocks(str(content)), markdown=str(content))],
            provider=self.name,
            inference_ms=elapsed,
            model=str(body.get("model") or model),
        )


def html_to_blocks(html: str) -> dict[str, Any]:
    """Rebuild PaddleOCR-VL's ``parsing_res_list`` from transcribed HTML.

    ``paddle_vl.to_payload`` reads blocks that carry a ``block_label`` and
    ``block_content``; it does not care who produced them. Producing that shape
    here is what lets a completely different reader drop into the pipeline
    without touching the converter, the role resolver, or the gates.
    """
    import re

    text = re.sub(r"^\s*```(?:html)?|```\s*$", "", html.strip(), flags=re.MULTILINE)
    blocks: list[dict[str, Any]] = []
    position = 0

    def paragraphs(chunk: str) -> None:
        for paragraph in re.split(r"</?p[^>]*>|<br\s*/?>|\n{2,}", chunk):
            stripped = re.sub(r"<[^>]+>", " ", paragraph)
            # Line breaks and tabs are tidied away; runs of spaces are not.
            # A page prints two fields on one line by putting white space
            # between them — "Invoice No: 1    Date: 12/27/2021" — and that gap
            # is the only evidence of where one field ends and the next begins.
            # Collapsing it to a single space merged them into one value.
            stripped = re.sub(r"[^\S ]+", " ", stripped).strip()
            if stripped:
                blocks.append({"block_label": "text", "block_content": stripped})

    for match in re.finditer(r"<table[\s\S]*?</table>", text, re.IGNORECASE):
        paragraphs(text[position:match.start()])
        blocks.append({"block_label": "table", "block_content": match.group(0)})
        position = match.end()
    paragraphs(text[position:])

    return {"parsing_res_list": blocks}


# ---------------------------------------------------------------------------
# Fallback wrapper
# ---------------------------------------------------------------------------
class FallbackProvider(AIProvider):
    """Try the GPU service; if it cannot be reached, read locally instead.

    Only :class:`AIUnavailable` falls through. A service that answered and said
    "no" is not retried elsewhere — that would just fail twice and take twice as
    long.
    """

    name = "http+local"

    def __init__(self, primary: AIProvider, backup: AIProvider) -> None:
        self._primary = primary
        self._backup = backup

    def health(self) -> tuple[bool, str]:
        ok, detail = self._primary.health()
        if ok:
            return True, f"{self._primary.name}: {detail}"
        backup_ok, backup_detail = self._backup.health()
        return backup_ok, f"{self._primary.name} down ({detail}); fallback {backup_detail}"

    # Why the fallback happened, kept for the caller to record on the job.
    # A silent fallback is the worst kind: the work still succeeds, so nobody
    # investigates, and a GPU that has been unreachable for a week goes unnoticed
    # while every page quietly takes ten times as long.
    last_fallback_reason: str = ""

    def read(self, image_path: Path) -> ReadOutcome:
        try:
            return self._primary.read(image_path)
        except AIUnavailable as error:
            self.last_fallback_reason = str(error)
            logger.warning("primary vision provider unavailable, falling back: %s", error)
            outcome = self._backup.read(image_path)
            outcome.provider = f"{self._backup.name} (fallback)"
            outcome.fallback_reason = str(error)
            return outcome

    def close(self) -> None:
        self._primary.close()
        self._backup.close()


def build_provider(settings) -> AIProvider:
    """The provider this deployment is configured for.

    Everything above is reachable through one environment variable, which is the
    whole point of the abstraction: a GPU host that shuts down, a model that
    turns out cheaper, a customer who forbids sending documents anywhere — each
    is a change to AI_PROVIDER, not to the pipeline.
    """
    local = LocalProvider(settings.worker_root)
    choice = settings.ai_provider

    if choice == "openrouter":
        remote: AIProvider = OpenRouterProvider(
            settings.openrouter_key, settings.openrouter_model,
            settings.ai_timeout_seconds, settings.openrouter_site,
            alternates=getattr(settings, "openrouter_alternates", ()),
        )
    elif choice == "http":
        remote = HttpProvider(
            settings.ai_service_url, settings.ai_service_token, settings.ai_timeout_seconds
        )
    else:
        return local

    return FallbackProvider(remote, local) if settings.ai_fallback_local else remote
