"""Read one page with several models and say which one got it right.

No catalogue entry tells you how a model reads an Arabic invoice. The
descriptions say "visual understanding" and "document analysis"; none of them
say whether the rate printed ٢٥٠٠٠ comes back as 25000 or as 2500. The only
answer that settles it is the one measured on the documents this customer
actually sends.

So this runs the same page through each candidate and reports, per model:

* what it cost, from the token counts the provider itself returned;
* how many line items came out, and whether the column roles resolved;
* whether the arithmetic holds — quantity x price against the line total, and
  the lines against the printed total. That last one is the real score: a
  reader that drops a digit fails it, and one that transcribes faithfully
  passes it without anybody reading the sheet.

Each reading is also written to ``dumps/`` so a disagreement can be examined
afterwards with ``ocr_worker/table_probe.py`` — without paying for it twice.

    python scripts/compare_readers.py invoice.png
    python scripts/compare_readers.py invoice.png qwen/qwen3.8-flash z-ai/glm-5.3-flash

Reads .env for OPENROUTER_API_KEY, so it costs real credit: one call per model.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The console this prints to is whatever the operator's terminal is — a Windows
# code page that cannot encode an Arabic invoice's own text, more often than
# not. A diagnostic must never die trying to describe itself.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

try:
    # Same as the service does at boot: verify TLS against the OS trust store,
    # which is what makes the price lookup work behind a corporate proxy.
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

# Candidates worth measuring, cheapest first. Prices are per million tokens and
# move, so they are printed from the live catalogue rather than trusted from
# here; this list only decides who gets tried.
DEFAULT_MODELS = (
    "qwen/qwen3.7-flash",
    "qwen/qwen3.8-flash",
    "qwen/qwen3.8-27b",
)


def _prices() -> dict[str, tuple[float, float]]:
    """(prompt, completion) price per million tokens, from OpenRouter itself."""
    import httpx

    try:
        response = httpx.get("https://openrouter.ai/api/v1/models", timeout=30)
        response.raise_for_status()
    except Exception as error:
        print(f"  (could not read the price list: {error})")
        return {}
    found: dict[str, tuple[float, float]] = {}
    for entry in (response.json() or {}).get("data") or []:
        pricing = entry.get("pricing") or {}
        try:
            found[str(entry.get("id"))] = (
                float(pricing.get("prompt") or 0) * 1_000_000,
                float(pricing.get("completion") or 0) * 1_000_000,
            )
        except (TypeError, ValueError):
            continue
    return found


def _read(model: str, image: Path, settings):
    from app.services.ai_provider import OpenRouterProvider

    provider = OpenRouterProvider(
        settings.openrouter_key, model, settings.ai_timeout_seconds,
        settings.openrouter_site, max_tokens=settings.openrouter_max_tokens,
    )
    started = time.perf_counter()
    outcome = provider.read(image)
    return outcome, int((time.perf_counter() - started) * 1000)


def _score(page: dict) -> dict:
    """What the gates make of this reading."""
    sys.path.insert(0, str(ROOT.parent / "ocr_worker"))
    import ai_extract
    import paddle_vl

    payload = paddle_vl.to_payload(page)
    document, blocking, _advisory = ai_extract.validate(payload, set())
    roles = {role for role in payload.get("column_roles") or [] if role != "other"}
    return {
        "items": len(document.get("items") or []),
        "roles": sorted(roles),
        "priced": {"qty", "unit_price", "line_total"} <= roles,
        "totals": document.get("totals") or {},
        "blocking": blocking,
    }


def compare(image: Path, models: list[str]) -> int:
    from app.core.config import get_settings

    settings = get_settings()
    if not settings.openrouter_key:
        print("OPENROUTER_API_KEY is empty in .env — nothing to compare with.")
        return 2

    dumps = Path("dumps")
    dumps.mkdir(exist_ok=True)
    prices = _prices()
    results = []

    for model in models:
        print(f"\n=== {model} ===")
        try:
            outcome, elapsed = _read(model, image, settings)
        except Exception as error:
            print(f"  failed: {type(error).__name__}: {error}")
            results.append({"model": model, "error": str(error)})
            continue

        page = outcome.pages[0].as_payload()
        name = model.replace("/", "_").replace(":", "_")
        (dumps / f"{image.stem}.{name}.json").write_text(
            json.dumps(page, ensure_ascii=False), encoding="utf-8"
        )

        prompt_tokens = getattr(outcome, "prompt_tokens", 0) or 0
        completion_tokens = getattr(outcome, "completion_tokens", 0) or 0
        cost = None
        if model in prices and (prompt_tokens or completion_tokens):
            per_prompt, per_completion = prices[model]
            cost = (prompt_tokens * per_prompt + completion_tokens * per_completion) / 1_000_000

        try:
            score = _score(page)
        except Exception as error:
            print(f"  read, but could not be understood: {type(error).__name__}: {error}")
            results.append({"model": model, "error": f"unreadable: {error}"})
            continue

        print(f"  {elapsed / 1000:.1f}s"
              + (f" · {prompt_tokens}+{completion_tokens} tokens" if prompt_tokens else "")
              + (f" · ${cost:.4f}" if cost is not None else ""))
        print(f"  items: {score['items']} · roles: {', '.join(score['roles']) or 'none'}")
        print(f"  totals: {score['totals']}")
        if score["blocking"]:
            for message in score["blocking"][:3]:
                print(f"  ✗ {message}")
        else:
            print("  ✓ the arithmetic holds")
        results.append({"model": model, "cost": cost, "elapsed": elapsed, **score})

    print("\n" + "=" * 72)
    print(f"{'model':32} {'items':>6} {'priced':>7} {'adds up':>8} {'cost':>9}")
    for row in results:
        if row.get("error"):
            print(f"{row['model']:32} {'—':>6} {'—':>7} {'—':>8} {'failed':>9}")
            continue
        cost = f"${row['cost']:.4f}" if row.get("cost") is not None else "?"
        print(f"{row['model']:32} {row['items']:>6} "
              f"{'yes' if row['priced'] else 'no':>7} "
              f"{'yes' if not row['blocking'] else 'no':>8} {cost:>9}")
    print("\nEach reading is in dumps/ — inspect one with:")
    print("  python ../ocr_worker/table_probe.py dumps/<file>.json")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    image = Path(argv[1])
    if not image.is_file():
        print(f"no such file: {image}")
        return 2
    return compare(image, list(argv[2:]) or list(DEFAULT_MODELS))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
