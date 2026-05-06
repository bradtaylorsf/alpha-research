"""Layered PDF extraction utility (issue #108).

Connectors that hit EDGAR, court opinions, ProPublica 990 attachments, FOIA
responses, and similar sources routinely return PDFs. Trafilatura strips
those to empty content, so without a dedicated path the agent silently loses
the document.

The pipeline tries the cheapest method that yields enough text and
escalates only when forced:

1. ``pypdf`` — text-based PDFs (fast, free, no system deps).
2. ``pdfplumber`` — handles structured layouts and renders tables to
   pipe-formatted markdown.
3. Tesseract OCR via ``pytesseract`` over pages rasterised with
   ``pypdfium2``. Skipped (with a WARN event) when the ``tesseract`` system
   binary is missing.
4. Opus 4.7 vision — escalation gated by ``RESEARCH_PDF_VLM_ESCALATION=1``.
   Costs real money, so the gate is opt-in *and* every escalation emits a
   WARN ``pdf_vlm_escalation`` event before the call so operators see when
   it fires.

A density gate (alpha chars per processed page) decides whether to fall to
the next layer. Page and char caps prevent a 500-page filing from blowing
the context window.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from research_agent import config
from research_agent.observability.events import emit
from research_agent.storage.jobs import Job

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

DEFAULT_MAX_PAGES = 100
DEFAULT_MAX_CHARS = 200_000

# Density threshold: a page that yields fewer than this many alpha chars on
# average across the sampled pages is treated as "this layer didn't work".
# 200 alpha chars / page sits comfortably below a normal text page (~2-3K
# chars) and well above the noise pypdf produces on a scanned PDF (typically
# 0-50 chars of stray glyph headers).
_DENSITY_MIN_ALPHA_PER_PAGE = 200

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# OCR rendering knobs. 200 DPI is the sweet spot for Tesseract: legibility
# without paying for 600-DPI rasterisation on every page.
_OCR_RENDER_DPI = 200
# Cap pages we send to the VLM separately — Opus vision calls are expensive
# and a 100-page filing isn't realistic to escalate in full.
_VLM_MAX_PAGES = 12

_USER_AGENT_DEFAULT = "research-agent/0.1"
_FETCH_TIMEOUT_S = 60.0


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _vlm_escalation_enabled() -> bool:
    raw = os.environ.get("RESEARCH_PDF_VLM_ESCALATION")
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY


def _user_agent() -> str:
    return config.get("RESEARCH_USER_AGENT") or _USER_AGENT_DEFAULT


# ---------------------------------------------------------------------------
# URL fetch + path resolution
# ---------------------------------------------------------------------------


def _looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _write_temp_pdf(data: bytes) -> Path:
    """Write ``data`` to a temp ``.pdf`` file and return its path.

    Uses ``mkstemp`` (not ``NamedTemporaryFile`` — we need to keep the file
    around past this call) and *closes* the OS file descriptor it hands back.
    Naïvely doing ``Path(tempfile.mkstemp(...)[1])`` leaks the descriptor on
    every call.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return Path(tmp_path)


async def fetch_pdf_bytes(url: str, *, timeout: float = _FETCH_TIMEOUT_S) -> bytes:
    """Fetch a PDF over HTTP(S) and return the raw bytes.

    Raises :class:`httpx.HTTPError` on transport failure so callers can decide
    whether to retry. Used by :func:`extract` when handed a URL — and exposed
    publicly so :mod:`web_fetch` can hand pre-fetched bytes through if it
    already has them.
    """
    headers = {"User-Agent": _user_agent()}
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers=headers,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


# ---------------------------------------------------------------------------
# Density / truncation helpers
# ---------------------------------------------------------------------------


def _alpha_count(text: str) -> int:
    return sum(1 for ch in text if ch.isalpha())


def _text_density(text: str, pages_processed: int) -> bool:
    """Return True when ``text`` has enough alpha chars per page to keep.

    ``pages_processed == 0`` means the layer didn't produce any pages at all
    (e.g. pypdf raised) — that always counts as failure.
    """
    if pages_processed <= 0:
        return False
    avg = _alpha_count(text) / pages_processed
    return avg >= _DENSITY_MIN_ALPHA_PER_PAGE


def _truncate(md: str, max_chars: int) -> str:
    if max_chars <= 0 or len(md) <= max_chars:
        return md
    return md[:max_chars].rstrip() + "\n\n…[truncated]"


# ---------------------------------------------------------------------------
# Layer 1 — pypdf
# ---------------------------------------------------------------------------


def _extract_pypdf(path: Path, max_pages: int) -> tuple[str, int]:
    """Return ``(markdown, pages_processed)`` from pypdf.

    Includes any document-level outline (bookmarks) as a top-level heading
    list so the synthesizer can cite specific sections; per-page text is
    grouped under ``## Page N`` headings.
    """
    import pypdf  # lazy

    try:
        reader = pypdf.PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001 — fall through to next layer
        logger.debug("pypdf open failed for %s: %s", path, exc)
        return "", 0

    total_pages = len(reader.pages)
    page_slice = min(total_pages, max_pages)

    parts: list[str] = []

    outline_md = _format_pypdf_outline(reader)
    if outline_md:
        parts.append(outline_md)

    pages_processed = 0
    for idx in range(page_slice):
        try:
            text = reader.pages[idx].extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            logger.debug("pypdf page %d extract failed: %s", idx, exc)
            text = ""
        text = text.strip()
        pages_processed += 1
        if not text:
            continue
        parts.append(f"## Page {idx + 1}\n\n{text}")

    return "\n\n".join(parts), pages_processed


def _format_pypdf_outline(reader: object) -> str:
    """Best-effort: render the PDF outline (bookmarks) as a markdown list."""
    try:
        outline = getattr(reader, "outline", None)
    except Exception:  # noqa: BLE001
        return ""
    if not outline:
        return ""

    lines: list[str] = []

    def _walk(items: object, depth: int) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, list):
                _walk(item, depth + 1)
                continue
            title = getattr(item, "title", None)
            if isinstance(title, str) and title.strip():
                lines.append(f"{'  ' * depth}- {title.strip()}")

    _walk(outline, 0)
    if not lines:
        return ""
    return "## Outline\n\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Layer 2 — pdfplumber (tables + structured layouts)
# ---------------------------------------------------------------------------


def _extract_pdfplumber(path: Path, max_pages: int) -> tuple[str, int]:
    """Return ``(markdown, pages_processed)`` from pdfplumber, including tables."""
    try:
        import pdfplumber  # lazy
    except Exception as exc:  # noqa: BLE001
        logger.debug("pdfplumber import failed: %s", exc)
        return "", 0

    parts: list[str] = []
    pages_processed = 0
    try:
        with pdfplumber.open(str(path)) as pdf:
            for idx, page in enumerate(pdf.pages):
                if idx >= max_pages:
                    break
                pages_processed += 1
                text = (page.extract_text() or "").strip()
                table_blocks: list[str] = []
                try:
                    tables = page.extract_tables() or []
                except Exception as exc:  # noqa: BLE001
                    logger.debug("pdfplumber tables failed on page %d: %s", idx, exc)
                    tables = []
                for table in tables:
                    md_table = _table_to_markdown(table)
                    if md_table:
                        table_blocks.append(md_table)

                if not text and not table_blocks:
                    continue

                section = [f"## Page {idx + 1}"]
                if text:
                    section.append(text)
                section.extend(table_blocks)
                parts.append("\n\n".join(section))
    except Exception as exc:  # noqa: BLE001
        logger.debug("pdfplumber failed for %s: %s", path, exc)
        return "\n\n".join(parts), pages_processed

    return "\n\n".join(parts), pages_processed


def _table_to_markdown(rows: list[list[str | None]]) -> str:
    """Render a pdfplumber table as a markdown pipe table.

    Empty rows / fully-empty tables return an empty string so callers can
    skip them without checking shape.
    """
    cleaned: list[list[str]] = []
    for row in rows:
        if not row:
            continue
        cleaned.append([(cell or "").strip().replace("\n", " ") for cell in row])
    if not cleaned:
        return ""
    width = max(len(r) for r in cleaned)
    header = cleaned[0] + [""] * (width - len(cleaned[0]))
    body = [r + [""] * (width - len(r)) for r in cleaned[1:]]

    lines = [
        "| " + " | ".join(_escape_cell(c) for c in header) + " |",
        "|" + "|".join("---" for _ in range(width)) + "|",
    ]
    for r in body:
        lines.append("| " + " | ".join(_escape_cell(c) for c in r) + " |")
    return "\n".join(lines)


_TABLE_PIPE_RE = re.compile(r"\|")


def _escape_cell(value: str) -> str:
    return _TABLE_PIPE_RE.sub("\\\\|", value)


# ---------------------------------------------------------------------------
# Layer 3 — Tesseract OCR
# ---------------------------------------------------------------------------


def _tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def _render_pages_to_images(path: Path, max_pages: int) -> list[object]:
    """Rasterise the first ``max_pages`` pages to PIL ``Image`` objects.

    Uses ``pypdfium2`` (no system deps) rather than ``pdf2image`` (needs
    poppler). Returns an empty list on any failure so callers can fall
    through.
    """
    try:
        import pypdfium2 as pdfium  # lazy
    except Exception as exc:  # noqa: BLE001
        logger.debug("pypdfium2 import failed: %s", exc)
        return []

    images: list[object] = []
    try:
        pdf = pdfium.PdfDocument(str(path))
        scale = _OCR_RENDER_DPI / 72.0
        for idx, page in enumerate(pdf):
            if idx >= max_pages:
                break
            try:
                bitmap = page.render(scale=scale)
                images.append(bitmap.to_pil())
            finally:
                page.close()
        pdf.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("pypdfium2 rasterise failed for %s: %s", path, exc)
        return images
    return images


def _extract_tesseract(path: Path, max_pages: int) -> tuple[str, int]:
    """Return ``(markdown, pages_processed)`` from Tesseract OCR.

    When the ``tesseract`` system binary isn't on PATH, returns ``("", 0)``
    so the caller logs a WARN and moves on. We never raise from this layer —
    a missing system binary is an ops issue, not a code error.
    """
    if not _tesseract_available():
        logger.warning("tesseract binary not found on PATH — skipping OCR layer")
        return "", 0

    try:
        import pytesseract  # lazy
    except Exception as exc:  # noqa: BLE001
        logger.debug("pytesseract import failed: %s", exc)
        return "", 0

    images = _render_pages_to_images(path, max_pages)
    if not images:
        return "", 0

    parts: list[str] = []
    for idx, image in enumerate(images):
        try:
            text = pytesseract.image_to_string(image) or ""
        except Exception as exc:  # noqa: BLE001
            logger.debug("pytesseract OCR failed on page %d: %s", idx, exc)
            text = ""
        text = text.strip()
        if not text:
            continue
        parts.append(f"## Page {idx + 1}\n\n{text}")

    return "\n\n".join(parts), len(images)


# ---------------------------------------------------------------------------
# Layer 4 — VLM escalation (Opus 4.7 vision)
# ---------------------------------------------------------------------------


def _emit_vlm_escalation(*, source: str, pages_sent: int, job: Job | None) -> None:
    """Emit the WARN before invoking the VLM so cost is always visible.

    When ``job`` is None (e.g. one-shot smoke or a unit test), fall back to
    ``logger.warning`` so the operator still sees the escalation in stderr.
    """
    payload = {
        "source": source,
        "pages_sent": pages_sent,
        "tier": "frontier",
        "model": "anthropic/claude-opus-4-7",
    }
    if job is not None:
        emit(job, "WARN", "pdf", "pdf_vlm_escalation", payload)
    else:
        logger.warning("pdf_vlm_escalation %s pages=%d", source, pages_sent)


def _extract_vlm(
    path: Path,
    max_pages: int,
    *,
    source: str,
    job: Job | None,
) -> tuple[str, int]:
    """Escalate to the Opus 4.7 vision tier.

    The router is constructed inline so this code path stays self-contained:
    callers shouldn't need to thread a Router through every PDF call. The
    WARN event is emitted before the network call so a crash mid-call still
    leaves a paper trail.
    """
    pages_sent = min(max_pages, _VLM_MAX_PAGES)
    images = _render_pages_to_images(path, pages_sent)
    if not images:
        return "", 0

    _emit_vlm_escalation(source=source, pages_sent=len(images), job=job)

    try:
        text = _run_vlm_call(images)
    except Exception as exc:  # noqa: BLE001 — VLM escalation is best-effort
        logger.warning("pdf VLM escalation failed: %s", exc)
        return "", len(images)
    return text, len(images)


def _images_to_data_urls(images: list[object]) -> list[str]:
    """Encode PIL images as PNG ``data:`` URLs for the chat-image payload."""
    import io

    out: list[str] = []
    for image in images:
        buf = io.BytesIO()
        image.save(buf, format="PNG")  # type: ignore[attr-defined]
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
        out.append(f"data:image/png;base64,{encoded}")
    return out


def _run_vlm_call(images: list[object]) -> str:
    """Invoke the frontier vision tier with rasterised pages.

    Hits the OpenRouter chat-completions endpoint directly with an image
    payload. We don't go through the Pydantic AI agent because vision input
    + plain markdown output is a one-shot call — adding the Agent wrapper
    here would couple PDF extraction to the orchestrator's runtime config.
    """
    import openai

    from research_agent.llm.router import OPENROUTER_BASE_URL, load_models_config

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY required for pdf VLM escalation"
        )

    models_cfg = load_models_config()
    spec = models_cfg["tiers"].get("frontier")
    if not spec:
        raise RuntimeError("frontier tier missing from models.yaml")
    model_name = spec["model"]

    client = openai.OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)
    image_urls = _images_to_data_urls(images)
    user_content: list[dict[str, object]] = [
        {
            "type": "text",
            "text": (
                "Transcribe the attached PDF pages into well-structured markdown. "
                "Preserve heading hierarchy, lists, and tables. Output markdown only."
            ),
        }
    ]
    for url in image_urls:
        user_content.append({"type": "image_url", "image_url": {"url": url}})

    completion = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": user_content}],  # type: ignore[arg-type, misc, list-item]
    )
    if not completion.choices:
        return ""
    return completion.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def extract(
    path_or_url: str | Path,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_chars: int = DEFAULT_MAX_CHARS,
    job: Job | None = None,
) -> str:
    """Extract markdown from a local PDF path or a remote URL.

    Tries pypdf → pdfplumber → Tesseract; escalates to the Opus 4.7 vision
    tier only when ``RESEARCH_PDF_VLM_ESCALATION`` is truthy *and* every
    cheaper layer fails the density gate.

    Returns the empty string when every layer fails to produce usable text;
    callers decide whether to record a :class:`Source` or skip the document.
    """
    raw = str(path_or_url)
    cleanup_temp: Path | None = None
    if _looks_like_url(raw):
        try:
            data = await fetch_pdf_bytes(raw)
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("pdf fetch failed for %s: %s", raw, exc)
            return ""
        tmp = _write_temp_pdf(data)
        cleanup_temp = tmp
        path = tmp
        source_label = raw
    else:
        path = Path(raw)
        source_label = str(path)
        if not path.exists():
            logger.warning("pdf path does not exist: %s", path)
            return ""

    try:
        return await asyncio.get_running_loop().run_in_executor(
            None,
            _extract_sync,
            path,
            max_pages,
            max_chars,
            source_label,
            job,
        )
    finally:
        if cleanup_temp is not None:
            try:
                cleanup_temp.unlink()
            except OSError:
                pass


def _extract_sync(
    path: Path,
    max_pages: int,
    max_chars: int,
    source_label: str,
    job: Job | None,
) -> str:
    """Synchronous core of :func:`extract` — runs every layer in turn.

    Pulled out of ``extract`` so callers without an event loop (the smoke
    helper, unit tests for individual layers) can drive the same logic
    without spinning up asyncio.
    """
    md, pages = _extract_pypdf(path, max_pages)
    if _text_density(md, pages):
        return _truncate(md, max_chars)

    md2, pages2 = _extract_pdfplumber(path, max_pages)
    if _text_density(md2, pages2):
        return _truncate(md2, max_chars)
    if len(md2) > len(md):
        md, pages = md2, pages2

    md3, pages3 = _extract_tesseract(path, max_pages)
    if _text_density(md3, pages3):
        return _truncate(md3, max_chars)
    if len(md3) > len(md):
        md, pages = md3, pages3

    if _vlm_escalation_enabled():
        md4, pages4 = _extract_vlm(path, max_pages, source=source_label, job=job)
        if md4.strip():
            return _truncate(md4, max_chars)

    return _truncate(md, max_chars)


def extract_from_bytes(
    data: bytes,
    *,
    source_label: str = "<bytes>",
    max_pages: int = DEFAULT_MAX_PAGES,
    max_chars: int = DEFAULT_MAX_CHARS,
    job: Job | None = None,
) -> str:
    """Run the layered pipeline against in-memory PDF bytes.

    Used by :mod:`web_fetch` when the upstream HTTP response was already a
    PDF — avoids re-downloading the document just to feed it to ``pypdf``.
    """
    if not data:
        return ""
    tmp = _write_temp_pdf(data)
    try:
        return _extract_sync(tmp, max_pages, max_chars, source_label, job)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def extract_sync(
    path_or_url: str | Path,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_chars: int = DEFAULT_MAX_CHARS,
    job: Job | None = None,
) -> str:
    """Blocking variant of :func:`extract` for code paths without asyncio.

    URLs are fetched via ``asyncio.run`` so this function works the same way
    from sync contexts (the CLI smoke verb, scripts) and async contexts
    needing a one-shot blocking call.
    """
    raw = str(path_or_url)
    if _looks_like_url(raw):
        try:
            data = asyncio.run(fetch_pdf_bytes(raw))
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("pdf fetch failed for %s: %s", raw, exc)
            return ""
        tmp = _write_temp_pdf(data)
        try:
            return _extract_sync(tmp, max_pages, max_chars, raw, job)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    path = Path(raw)
    if not path.exists():
        logger.warning("pdf path does not exist: %s", path)
        return ""
    return _extract_sync(path, max_pages, max_chars, str(path), job)


__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_PAGES",
    "extract",
    "extract_from_bytes",
    "extract_sync",
    "fetch_pdf_bytes",
]
