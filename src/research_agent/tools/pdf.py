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
# Local corpus indexing may ingest large FOIA / archive PDFs whose
# document-level fidelity matters; the corpus-scoped defaults lift the
# per-document caps above the connector defaults to fit those payloads.
CORPUS_MAX_PAGES = 1000
CORPUS_MAX_CHARS = 2_000_000
# Absolute clamp shared by the local_corpus resolver so a misconfigured
# intake value can't push extraction beyond the supported range.
MAX_PAGES_HARD_CAP = 1000

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
# Hybrid per-page mode — merge text-layer + OCR within each page
# ---------------------------------------------------------------------------


def _ocr_pages_texts(path: Path, max_pages: int) -> list[str]:
    """Return per-page Tesseract OCR strings (empty list if OCR unavailable).

    Each entry is the OCR output for page ``i`` (1-indexed by convention
    in the caller). Pages where OCR fails get an empty string so the
    caller can align indices with the text-layer extraction.
    """
    if not _tesseract_available():
        return []

    try:
        import pytesseract  # lazy
    except Exception as exc:  # noqa: BLE001
        logger.debug("pytesseract import failed: %s", exc)
        return []

    images = _render_pages_to_images(path, max_pages)
    texts: list[str] = []
    for image in images:
        try:
            text = pytesseract.image_to_string(image) or ""
        except Exception as exc:  # noqa: BLE001
            logger.debug("pytesseract OCR failed on page image: %s", exc)
            text = ""
        texts.append(text.strip())
    return texts


def _pdfplumber_page_layers(path: Path, max_pages: int) -> list[tuple[str, list[str]]]:
    """Return ``[(page_text, table_markdown_blocks), ...]`` per page index.

    Empty list when pdfplumber is unavailable or the PDF is unreadable.
    The hybrid path uses table blocks alongside text-layer extraction so
    structured tables on typed pages render as markdown rather than
    collapsing into a noisy text stream.
    """
    try:
        import pdfplumber  # lazy
    except Exception as exc:  # noqa: BLE001
        logger.debug("pdfplumber import failed: %s", exc)
        return []

    layers: list[tuple[str, list[str]]] = []
    try:
        with pdfplumber.open(str(path)) as pdf:
            for idx, page in enumerate(pdf.pages):
                if idx >= max_pages:
                    break
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
                layers.append((text, table_blocks))
    except Exception as exc:  # noqa: BLE001
        logger.debug("pdfplumber failed for %s: %s", path, exc)
        return layers

    return layers


def _merge_hybrid_page_body(text: str, table_blocks: list[str], ocr_text: str) -> str:
    """Compose one page's hybrid body from text + table + OCR layers.

    When both layers produced content, the body is split into
    ``### Text layer`` and ``### OCR supplement`` sections so the operator
    can see which content came from where. When only one layer produced
    content, the body is that content with no subsections (no value in a
    single-section header).
    """
    layer_parts: list[str] = []
    if text.strip():
        layer_parts.append(text.strip())
    layer_parts.extend(block for block in table_blocks if block.strip())
    body = "\n\n".join(layer_parts)
    ocr = ocr_text.strip()

    if body and ocr:
        return f"### Text layer\n\n{body}\n\n### OCR supplement\n\n{ocr}"
    if body:
        return body
    return ocr


def _extract_hybrid_pages(path: Path, max_pages: int) -> tuple[str, int]:
    """Return ``(markdown, pages_with_content)`` from a per-page hybrid pass.

    Unlike the document-scope layered pipeline (pypdf → pdfplumber →
    Tesseract → VLM, which picks one layer for the whole file), this
    helper processes every page up to ``max_pages`` independently and
    merges its text-layer body, pdfplumber tables, and Tesseract OCR
    under ``## Page N`` headings. That fixes the failure mode where a
    document mixes typed sections (caught by pypdf) with scanned inserts
    (which pypdf silently drops because the typed half passed the
    density gate).
    """
    import pypdf  # lazy

    try:
        reader = pypdf.PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.debug("pypdf open failed for hybrid extract %s: %s", path, exc)
        return "", 0

    page_count = min(len(reader.pages), max_pages)
    if page_count <= 0:
        return "", 0

    parts: list[str] = []
    outline_md = _format_pypdf_outline(reader)
    if outline_md:
        parts.append(outline_md)

    plumber_layers = _pdfplumber_page_layers(path, page_count)
    ocr_texts = _ocr_pages_texts(path, page_count)

    pages_with_content = 0
    for idx in range(page_count):
        try:
            text = reader.pages[idx].extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            logger.debug("pypdf page %d extract failed: %s", idx, exc)
            text = ""
        text = text.strip()

        plumber_text = ""
        table_blocks: list[str] = []
        if idx < len(plumber_layers):
            plumber_text, table_blocks = plumber_layers[idx]
            if plumber_text and not text:
                text = plumber_text
            elif plumber_text and plumber_text not in text:
                text = f"{text}\n\n{plumber_text}".strip()

        ocr = ocr_texts[idx] if idx < len(ocr_texts) else ""
        merged = _merge_hybrid_page_body(text, table_blocks, ocr)
        if not merged:
            continue
        parts.append(f"## Page {idx + 1}\n\n{merged}")
        pages_with_content += 1

    return "\n\n".join(parts), pages_with_content or page_count


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
    hybrid_pages: bool = False,
) -> str:
    """Extract markdown from a local PDF path or a remote URL.

    With ``hybrid_pages=False`` (default), tries pypdf → pdfplumber →
    Tesseract at document scope; escalates to the Opus 4.7 vision tier
    only when ``RESEARCH_PDF_VLM_ESCALATION`` is truthy *and* every
    cheaper layer fails the density gate.

    With ``hybrid_pages=True``, processes every page (up to ``max_pages``)
    independently and merges text-layer + OCR content under ``## Page N``
    headings (issue #374). Use this for documents that mix typed sections
    with scanned inserts — FOIA responses, court exhibits, archival scans
    — where document-scope extraction silently drops one half.

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
            lambda: _extract_sync(
                path,
                max_pages,
                max_chars,
                source_label,
                job,
                hybrid_pages=hybrid_pages,
            ),
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
    *,
    hybrid_pages: bool = False,
) -> str:
    """Synchronous core of :func:`extract` — runs every layer in turn.

    Pulled out of ``extract`` so callers without an event loop (the smoke
    helper, unit tests for individual layers) can drive the same logic
    without spinning up asyncio.

    When ``hybrid_pages`` is true, short-circuits to the per-page merge
    pipeline before any document-scope layer fires. Falls back to the
    layered document-scope pipeline if hybrid mode produced nothing.
    """
    if hybrid_pages:
        md, _pages = _extract_hybrid_pages(path, max_pages)
        if md.strip():
            return _truncate(md, max_chars)

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
    hybrid_pages: bool = False,
) -> str:
    """Run the layered pipeline against in-memory PDF bytes.

    Used by :mod:`web_fetch` when the upstream HTTP response was already a
    PDF — avoids re-downloading the document just to feed it to ``pypdf``.
    ``hybrid_pages`` matches :func:`extract` semantics.
    """
    if not data:
        return ""
    tmp = _write_temp_pdf(data)
    try:
        return _extract_sync(
            tmp,
            max_pages,
            max_chars,
            source_label,
            job,
            hybrid_pages=hybrid_pages,
        )
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
    hybrid_pages: bool = False,
) -> str:
    """Blocking variant of :func:`extract` for code paths without asyncio.

    URLs are fetched via ``asyncio.run`` so this function works the same way
    from sync contexts (the CLI smoke verb, scripts) and async contexts
    needing a one-shot blocking call. ``hybrid_pages`` matches
    :func:`extract` semantics.
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
            return _extract_sync(
                tmp,
                max_pages,
                max_chars,
                raw,
                job,
                hybrid_pages=hybrid_pages,
            )
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    path = Path(raw)
    if not path.exists():
        logger.warning("pdf path does not exist: %s", path)
        return ""
    return _extract_sync(
        path,
        max_pages,
        max_chars,
        str(path),
        job,
        hybrid_pages=hybrid_pages,
    )


# ---------------------------------------------------------------------------
# Section walk (issue #206) — structural splitting for cornerstone PDFs
# ---------------------------------------------------------------------------

# Heading-detection regexes for the fallback path when the PDF outline is
# missing or sparse. Patterns are deliberately conservative — false positives
# here turn body paragraphs into pseudo-section breaks. ``Section`` requires a
# trailing digit + period to avoid matching ordinary mid-sentence prose.
_HEADING_REGEXES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^#{1,3}\s+(?P<title>.+)$"),
    re.compile(r"^(?P<title>Chapter\s+\d+\b.*)$", re.IGNORECASE),
    re.compile(r"^(?P<title>Section\s+\d+\b.*)$", re.IGNORECASE),
    re.compile(r"^(?P<title>Part\s+[IVX]+\b.*)$"),
    re.compile(r"^(?P<title>[IVX]{1,5}\.\s+.+)$"),
)

# Sliding-window fallback for unstructured PDFs (Stage 5). Smaller than
# DEFAULT_MAX_CHARS so each window fits comfortably and overlap preserves
# claims that straddle a boundary.
_WINDOW_CHARS = 150_000
_WINDOW_OVERLAP_CHARS = 10_000

_MIN_STRUCTURED_SECTIONS = 3


def extract_sections_sync(
    path_or_url: str | Path,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_chars_per_section: int = DEFAULT_MAX_CHARS,
    job: Job | None = None,
    doc_title: str | None = None,
) -> list[dict[str, object]]:
    """Return a structural section-walk of a PDF (issue #206).

    Each section is ``{"breadcrumb", "text", "page_start", "page_end",
    "structured"}``. Sections are derived from (in order of preference):

    1. The pypdf outline (bookmarks → page numbers via
       ``reader.get_destination_page_number``).
    2. Heading regex matches on the per-page text when the outline is
       missing or yields fewer than :data:`_MIN_STRUCTURED_SECTIONS`.
    3. A sliding 150k-char window with 10k overlap when neither of the
       above produces enough sections — flagged with ``structured=False``
       so the caller can dedupe by claim-text Jaccard similarity.

    Each section's body is capped at ``max_chars_per_section``. Sections
    that exceed the cap are themselves split into the sliding-window
    representation so a 600-page chapter doesn't get truncated.
    """
    raw = str(path_or_url)
    cleanup_temp: Path | None = None
    if _looks_like_url(raw):
        try:
            data = asyncio.run(fetch_pdf_bytes(raw))
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("pdf fetch failed for %s: %s", raw, exc)
            return []
        path = _write_temp_pdf(data)
        cleanup_temp = path
    else:
        path = Path(raw)
        if not path.exists():
            logger.warning("pdf path does not exist: %s", path)
            return []
    try:
        return _build_sections(
            path,
            max_pages=max_pages,
            max_chars_per_section=max_chars_per_section,
            doc_title=doc_title or path.stem,
        )
    finally:
        if cleanup_temp is not None:
            try:
                cleanup_temp.unlink()
            except OSError:
                pass


async def extract_sections(
    path_or_url: str | Path,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_chars_per_section: int = DEFAULT_MAX_CHARS,
    job: Job | None = None,
    doc_title: str | None = None,
) -> list[dict[str, object]]:
    """Async wrapper around :func:`extract_sections_sync` for use inside the loop.

    Mirrors :func:`extract`: I/O happens in a thread executor so a 920-page
    PDF doesn't stall the event loop while pypdf walks pages.
    """
    raw = str(path_or_url)
    cleanup_temp: Path | None = None
    if _looks_like_url(raw):
        try:
            data = await fetch_pdf_bytes(raw)
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("pdf fetch failed for %s: %s", raw, exc)
            return []
        path = _write_temp_pdf(data)
        cleanup_temp = path
    else:
        path = Path(raw)
        if not path.exists():
            logger.warning("pdf path does not exist: %s", path)
            return []
    try:
        return await asyncio.get_running_loop().run_in_executor(
            None,
            _build_sections,
            path,
            max_pages,
            max_chars_per_section,
            doc_title or path.stem,
        )
    finally:
        if cleanup_temp is not None:
            try:
                cleanup_temp.unlink()
            except OSError:
                pass


def _build_sections(
    path: Path,
    max_pages: int,
    max_chars_per_section: int,
    doc_title: str,
) -> list[dict[str, object]]:
    """Synchronous core of the section walk — runs pypdf + section assembly."""
    import pypdf  # lazy

    try:
        reader = pypdf.PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.debug("pypdf open failed for %s during section walk: %s", path, exc)
        return []

    total_pages = len(reader.pages)
    page_slice = min(total_pages, max_pages)
    page_texts: list[str] = []
    for idx in range(page_slice):
        try:
            text = reader.pages[idx].extract_text() or ""
        except Exception:  # noqa: BLE001
            text = ""
        page_texts.append(text)

    outline_anchors = _outline_to_anchors(reader, page_slice)
    sections: list[dict[str, object]] = []
    if len(outline_anchors) >= _MIN_STRUCTURED_SECTIONS:
        sections = _slice_by_anchors(
            page_texts, outline_anchors, doc_title=doc_title
        )
    else:
        regex_anchors = _heading_anchors_from_pages(page_texts)
        if len(regex_anchors) >= _MIN_STRUCTURED_SECTIONS:
            sections = _slice_by_anchors(
                page_texts, regex_anchors, doc_title=doc_title
            )

    if len(sections) >= _MIN_STRUCTURED_SECTIONS:
        return _split_oversized_sections(sections, max_chars_per_section)

    # Stage 5: sliding-window fallback over the whole document.
    return _windows_for_unstructured(page_texts, doc_title=doc_title)


def _outline_to_anchors(reader: object, page_slice: int) -> list[tuple[int, list[str]]]:
    """Walk the pypdf outline; return ``(page_index, breadcrumb_path)`` anchors.

    The outline is a nested list of ``Destination`` objects; nested lists
    represent depth. We resolve each destination to its 0-based page index
    via ``reader.get_destination_page_number`` (the public pypdf API for
    this) and carry the parent titles as breadcrumb context.
    """
    try:
        outline = getattr(reader, "outline", None)
    except Exception:  # noqa: BLE001
        return []
    if not outline:
        return []

    resolver = getattr(reader, "get_destination_page_number", None)
    anchors: list[tuple[int, list[str]]] = []

    def _walk(items: object, parents: list[str]) -> None:
        if not isinstance(items, list):
            return
        current_parents = list(parents)
        last_title: str | None = None
        for item in items:
            if isinstance(item, list):
                # Nested list deepens the breadcrumb under the most recent sibling.
                child_parents = (
                    current_parents + [last_title] if last_title else current_parents
                )
                _walk(item, child_parents)
                continue
            title = getattr(item, "title", None)
            if not isinstance(title, str) or not title.strip():
                continue
            title = title.strip()
            last_title = title
            if resolver is None:
                continue
            try:
                page_idx = resolver(item)
            except Exception:  # noqa: BLE001 — destination may be malformed
                continue
            if not isinstance(page_idx, int):
                continue
            if page_idx < 0 or page_idx >= page_slice:
                continue
            anchors.append((page_idx, current_parents + [title]))

    _walk(outline, [])
    anchors.sort(key=lambda a: a[0])
    return anchors


def _heading_anchors_from_pages(
    page_texts: list[str],
) -> list[tuple[int, list[str]]]:
    """Regex-based heading detection over per-page text. Page-level granularity."""
    anchors: list[tuple[int, list[str]]] = []
    for idx, text in enumerate(page_texts):
        if not text:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or len(stripped) > 200:
                continue
            for pattern in _HEADING_REGEXES:
                m = pattern.match(stripped)
                if m:
                    title = m.group("title").strip()
                    if title:
                        anchors.append((idx, [title]))
                    break
    return anchors


def _slice_by_anchors(
    page_texts: list[str],
    anchors: list[tuple[int, list[str]]],
    *,
    doc_title: str,
) -> list[dict[str, object]]:
    """Slice ``page_texts`` between consecutive anchor pages into sections."""
    if not anchors:
        return []

    # Ensure anchors are in page order; tolerate duplicates on the same page.
    sorted_anchors = sorted(anchors, key=lambda a: a[0])
    sections: list[dict[str, object]] = []
    for i, (page_idx, breadcrumb_path) in enumerate(sorted_anchors):
        page_start = page_idx
        page_end = (
            sorted_anchors[i + 1][0] - 1
            if i + 1 < len(sorted_anchors)
            else len(page_texts) - 1
        )
        if page_end < page_start:
            page_end = page_start
        body = "\n\n".join(
            page_texts[p].strip()
            for p in range(page_start, page_end + 1)
            if page_texts[p].strip()
        )
        if not body:
            continue
        breadcrumb = " > ".join([doc_title, *breadcrumb_path])
        breadcrumb = (
            f"{breadcrumb} (pages {page_start + 1}-{page_end + 1})"
        )
        sections.append(
            {
                "breadcrumb": breadcrumb,
                "text": body,
                "page_start": page_start + 1,
                "page_end": page_end + 1,
                "structured": True,
            }
        )
    return sections


def _split_oversized_sections(
    sections: list[dict[str, object]],
    max_chars_per_section: int,
) -> list[dict[str, object]]:
    """Replace any section whose body exceeds ``max_chars_per_section`` with windows.

    Issue #206: a section larger than the per-section cap falls back to the
    sliding-window representation so the cornerstone-extract prompt still
    sees the whole body across multiple LLM calls.
    """
    if max_chars_per_section <= 0:
        return sections
    out: list[dict[str, object]] = []
    for section in sections:
        text = section["text"]
        if not isinstance(text, str) or len(text) <= max_chars_per_section:
            out.append(section)
            continue
        page_start = int(section.get("page_start", 0) or 0)
        page_end = int(section.get("page_end", page_start) or page_start)
        breadcrumb = str(section.get("breadcrumb", ""))
        for window_idx, (start, end, window_text) in enumerate(
            _slide_windows(text), start=1
        ):
            window_breadcrumb = (
                f"{breadcrumb} > window {window_idx} (chars {start}-{end})"
            )
            out.append(
                {
                    "breadcrumb": window_breadcrumb,
                    "text": window_text,
                    "page_start": page_start,
                    "page_end": page_end,
                    "structured": False,
                }
            )
    return out


def _windows_for_unstructured(
    page_texts: list[str],
    *,
    doc_title: str,
) -> list[dict[str, object]]:
    """Fallback Stage-5 sliding-window split when no structure is detectable."""
    body = "\n\n".join(p for p in page_texts if p)
    if not body.strip():
        return []
    page_start = 1
    page_end = len(page_texts)
    sections: list[dict[str, object]] = []
    for idx, (start, end, window_text) in enumerate(
        _slide_windows(body), start=1
    ):
        sections.append(
            {
                "breadcrumb": (
                    f"{doc_title} > window {idx} (chars {start}-{end})"
                ),
                "text": window_text,
                "page_start": page_start,
                "page_end": page_end,
                "structured": False,
            }
        )
    return sections


def _slide_windows(
    text: str,
    *,
    window: int = _WINDOW_CHARS,
    overlap: int = _WINDOW_OVERLAP_CHARS,
) -> list[tuple[int, int, str]]:
    """Yield ``(char_start, char_end, window_text)`` slices over ``text``."""
    if window <= 0 or overlap < 0 or overlap >= window:
        raise ValueError("window must be > 0 and 0 <= overlap < window")
    n = len(text)
    if n == 0:
        return []
    step = window - overlap
    out: list[tuple[int, int, str]] = []
    start = 0
    while start < n:
        end = min(start + window, n)
        out.append((start, end, text[start:end]))
        if end >= n:
            break
        start += step
    return out


__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_PAGES",
    "extract",
    "extract_from_bytes",
    "extract_sections",
    "extract_sections_sync",
    "extract_sync",
    "fetch_pdf_bytes",
]
