"""Layered image OCR / extraction utility (issue #109).

Use case: FOIA responses scanned to images, news articles with embedded
charts, social-media screenshots used as evidence. Trafilatura silently
returns empty content for image bytes, so without a dedicated path the
agent loses the document.

The pipeline tries the cheapest method first and escalates only when the
text it produces is too thin to trust:

1. Tesseract via ``pytesseract`` — fast and free for typed text in
   screenshots / scanned forms. Skipped (with a WARN log) when the
   ``tesseract`` system binary isn't on PATH.
2. Local VLM via LM Studio — when one is loaded against the ``vision``
   tier in :mod:`config/models.yaml` (e.g. ``qwen3-vl-8b-instruct``).
   Better at handwriting, multi-column layouts, and lightly stylised
   text. Best-effort: any failure (no model loaded, connection refused)
   returns ``""`` so the pipeline can continue.
3. Opus 4.7 vision (frontier tier) — escalation gated by
   ``RESEARCH_OCR_VLM_ESCALATION=1``. Costs real money, so the gate is
   opt-in *and* every escalation emits a WARN ``ocr_vlm_escalation``
   event before the call.

Average word-confidence (Tesseract) decides whether to fall through:
when the average is below ``conf_threshold`` (default 0.7) we treat the
Tesseract output as unreliable and try the local VLM next.

**Limitation:** chart / data-visualization extraction is its own gnarly
problem (axis values, legend mapping, encoded series). v1 only attempts
to recognise *text* embedded in the image; charts are out of scope and
will produce only their rendered labels / titles, not their underlying
data.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

from research_agent import config
from research_agent.observability.events import emit
from research_agent.storage.jobs import Job

logger = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 200_000
DEFAULT_CONF_THRESHOLD = 0.7

_TRUTHY = frozenset({"1", "true", "yes", "on"})

_USER_AGENT_DEFAULT = "research-agent/0.1"
_FETCH_TIMEOUT_S = 60.0

_KNOWN_IMAGE_SUFFIXES: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_DEFAULT_IMAGE_SUFFIX = ".png"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _vlm_escalation_enabled() -> bool:
    raw = os.environ.get("RESEARCH_OCR_VLM_ESCALATION")
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY


def _user_agent() -> str:
    return config.get("RESEARCH_USER_AGENT") or _USER_AGENT_DEFAULT


def _looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


# ---------------------------------------------------------------------------
# URL fetch + temp-file helpers
# ---------------------------------------------------------------------------


def _suffix_for(url: str) -> str:
    """Pick a temp-file suffix from the URL path so PIL can sniff it.

    Falls back to ``.png`` for unknown URLs (PIL handles all common image
    formats regardless of suffix, but a suffix helps downstream tools and
    keeps temp files self-describing in logs).
    """
    path = urlparse(url).path.lower()
    for ext in _KNOWN_IMAGE_SUFFIXES:
        if path.endswith(ext):
            return ext
    return _DEFAULT_IMAGE_SUFFIX


def _write_temp_image(data: bytes, suffix: str = _DEFAULT_IMAGE_SUFFIX) -> Path:
    """Write ``data`` to a temp image file and return its path.

    Mirrors :func:`research_agent.tools.pdf._write_temp_pdf` — uses
    ``mkstemp`` because we need to keep the file around past this call
    and explicitly closes the OS file descriptor so we don't leak fds on
    every call.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return Path(tmp_path)


async def fetch_image_bytes(url: str, *, timeout: float = _FETCH_TIMEOUT_S) -> bytes:
    """Fetch an image over HTTP(S) and return the raw bytes.

    Raises :class:`httpx.HTTPError` on transport failure so callers can
    decide whether to retry. Public so :mod:`web_fetch` can pre-fetch
    bytes itself when a server-declared image content-type lands.
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
# Truncation helper
# ---------------------------------------------------------------------------


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n…[truncated]"


# ---------------------------------------------------------------------------
# Layer 1 — Tesseract
# ---------------------------------------------------------------------------


def _tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def _open_image(path: Path) -> object | None:
    """Open ``path`` as a PIL image; return None on any failure."""
    try:
        from PIL import Image  # lazy
    except Exception as exc:  # noqa: BLE001
        logger.debug("PIL import failed: %s", exc)
        return None
    try:
        return Image.open(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.debug("PIL open failed for %s: %s", path, exc)
        return None


def _average_word_confidence(confidences: list[int]) -> float:
    """Return the average of valid word confidences, scaled to ``[0, 1]``.

    Tesseract emits ``-1`` for whitespace / non-word entries; those are
    sentinels, not real measurements, and skew the average heavily if
    folded in. With no valid entries we return ``0.0`` so callers treat
    the layer as "no signal" and fall through.
    """
    valid = [c for c in confidences if c >= 0]
    if not valid:
        return 0.0
    return (sum(valid) / len(valid)) / 100.0


def _extract_tesseract(path: Path, conf_threshold: float) -> tuple[str, float]:
    """Return ``(text, avg_confidence)`` from Tesseract OCR.

    ``conf_threshold`` is accepted for symmetry with the public API but
    not consulted here — the threshold gate runs in :func:`_extract_sync`
    so the layer itself stays deterministic and unit-testable. We never
    raise: a missing system binary is an ops issue and returns
    ``("", 0.0)`` so the caller logs and moves on to the next layer.
    """
    del conf_threshold  # threshold is applied by the pipeline, not the layer

    if not _tesseract_available():
        logger.warning("tesseract binary not found on PATH — skipping OCR layer")
        return "", 0.0

    try:
        import pytesseract  # lazy
    except Exception as exc:  # noqa: BLE001
        logger.debug("pytesseract import failed: %s", exc)
        return "", 0.0

    image = _open_image(path)
    if image is None:
        return "", 0.0

    try:
        data = pytesseract.image_to_data(
            image,
            output_type=pytesseract.Output.DICT,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("pytesseract image_to_data failed for %s: %s", path, exc)
        return "", 0.0

    texts = data.get("text") or []
    confs_raw = data.get("conf") or []
    confs: list[int] = []
    for c in confs_raw:
        try:
            confs.append(int(float(c)))
        except (TypeError, ValueError):
            confs.append(-1)

    words: list[str] = []
    for token, conf in zip(texts, confs, strict=False):
        if conf < 0:
            continue
        token = (token or "").strip()
        if not token:
            continue
        words.append(token)

    avg = _average_word_confidence(confs)
    return "\n".join(words), avg


# ---------------------------------------------------------------------------
# Image → data-URL helper (shared by VLM layers)
# ---------------------------------------------------------------------------


def _image_to_data_url(path: Path) -> str | None:
    """Encode ``path`` as a base64 ``data:image/...;base64,...`` URL.

    The MIME type is derived from the file suffix (defaulting to
    ``image/png``) so LM Studio and OpenRouter both accept the payload
    without a separate content-type negotiation step.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.debug("read failed for %s: %s", path, exc)
        return None
    if not raw:
        return None
    suffix = path.suffix.lower().lstrip(".") or "png"
    if suffix == "jpg":
        suffix = "jpeg"
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:image/{suffix};base64,{encoded}"


_VLM_PROMPT = (
    "Transcribe every piece of text visible in the attached image into "
    "well-structured markdown. Preserve heading hierarchy, lists, and "
    "tables. If the image is a chart with axis labels or a legend, "
    "transcribe the labels but do not attempt to reconstruct the "
    "underlying data. Output markdown only."
)


# ---------------------------------------------------------------------------
# Layer 2 — Local VLM (LM Studio, vision tier)
# ---------------------------------------------------------------------------


def _extract_local_vlm(path: Path) -> str:
    """Best-effort local VLM transcription via LM Studio.

    Reads the ``vision`` tier from :mod:`config/models.yaml` and hits
    LM Studio's chat-completions endpoint with a base64 image payload.
    Any failure (config missing, connection refused, no model loaded,
    timeout) returns ``""`` — the local VLM is opportunistic; we never
    want a missing local model to block the pipeline from escalating to
    the cloud (or returning the Tesseract baseline).
    """
    try:
        import openai  # lazy

        from research_agent.llm.router import (
            LMSTUDIO_DEFAULT_BASE_URL,
            load_models_config,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("local VLM dependencies unavailable: %s", exc)
        return ""

    try:
        models_cfg = load_models_config()
    except Exception as exc:  # noqa: BLE001
        logger.debug("load_models_config failed: %s", exc)
        return ""

    spec = models_cfg.get("tiers", {}).get("vision")
    if not spec or spec.get("provider") != "lmstudio":
        logger.debug("vision tier not configured for lmstudio — skipping local VLM")
        return ""

    model_name = spec.get("model")
    if not model_name:
        return ""

    data_url = _image_to_data_url(path)
    if data_url is None:
        return ""

    base_url = config.get("LMSTUDIO_BASE_URL") or LMSTUDIO_DEFAULT_BASE_URL
    timeout_s = float(spec.get("timeout_s") or 60)

    try:
        client = openai.OpenAI(
            base_url=base_url,
            api_key="lm-studio",
            timeout=timeout_s,
        )
        completion = client.chat.completions.create(
            model=model_name,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _VLM_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001 — local VLM is best-effort
        logger.debug("local VLM call failed: %s", exc)
        return ""

    if not completion.choices:
        return ""
    return (completion.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# Layer 3 — Opus 4.7 vision (frontier tier)
# ---------------------------------------------------------------------------


def _emit_vlm_escalation(*, source: str, job: Job | None) -> None:
    """Emit the WARN before invoking the cloud VLM so cost is always visible."""
    payload = {
        "source": source,
        "tier": "frontier",
        "model": "anthropic/claude-opus-4-7",
    }
    if job is not None:
        emit(job, "WARN", "ocr", "ocr_vlm_escalation", payload)
    else:
        logger.warning("ocr_vlm_escalation %s", source)


def _extract_vlm(path: Path, *, source: str, job: Job | None) -> str:
    """Escalate to the Opus 4.7 vision tier.

    The router is constructed inline so this code path stays self-
    contained — callers shouldn't have to thread a Router through every
    OCR call. The WARN event is emitted *before* the network call so a
    crash mid-call still leaves a paper trail.
    """
    _emit_vlm_escalation(source=source, job=job)

    try:
        return _run_vlm_call(path)
    except Exception as exc:  # noqa: BLE001 — VLM escalation is best-effort
        logger.warning("ocr VLM escalation failed: %s", exc)
        return ""


def _run_vlm_call(path: Path) -> str:
    """Invoke the frontier vision tier with a single image.

    Hits OpenRouter chat-completions directly (no Pydantic AI agent
    wrapper) — vision-in / markdown-out is a one-shot, and avoiding the
    Agent layer keeps OCR independent of the orchestrator's runtime
    config.
    """
    import openai

    from research_agent.llm.router import OPENROUTER_BASE_URL, load_models_config

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY required for ocr VLM escalation")

    models_cfg = load_models_config()
    spec = models_cfg["tiers"].get("frontier")
    if not spec:
        raise RuntimeError("frontier tier missing from models.yaml")
    model_name = spec["model"]

    data_url = _image_to_data_url(path)
    if data_url is None:
        return ""

    client = openai.OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)
    user_content: list[dict[str, object]] = [
        {"type": "text", "text": _VLM_PROMPT},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    completion = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": user_content}],  # type: ignore[arg-type, misc, list-item]
    )
    if not completion.choices:
        return ""
    return (completion.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def _extract_sync(
    path: Path,
    max_chars: int,
    conf_threshold: float,
    source_label: str,
    job: Job | None,
) -> str:
    """Synchronous core of :func:`extract` — runs every layer in turn.

    Pulled out of ``extract`` so callers without an event loop (the
    smoke helper, unit tests for individual layers) can drive the same
    logic without spinning up asyncio.
    """
    tesseract_text, avg_conf = _extract_tesseract(path, conf_threshold)
    if tesseract_text and avg_conf >= conf_threshold:
        return _truncate(tesseract_text, max_chars)

    local_text = _extract_local_vlm(path)
    if local_text:
        return _truncate(local_text, max_chars)

    if _vlm_escalation_enabled():
        cloud_text = _extract_vlm(path, source=source_label, job=job)
        if cloud_text:
            return _truncate(cloud_text, max_chars)

    # No layer produced confident text; fall back to whatever Tesseract
    # managed (may be empty) so callers at least see best-effort output.
    return _truncate(tesseract_text, max_chars)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def extract(
    path_or_url: str | Path,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    conf_threshold: float = DEFAULT_CONF_THRESHOLD,
    job: Job | None = None,
) -> str:
    """Extract markdown of recognised text from a local image or URL.

    Tries Tesseract → local VLM → Opus 4.7 vision (gated on
    ``RESEARCH_OCR_VLM_ESCALATION``). Returns the empty string when
    every layer fails to produce usable text; callers decide whether to
    record a :class:`Source` or skip the document.
    """
    raw = str(path_or_url)
    cleanup_temp: Path | None = None

    if _looks_like_url(raw):
        try:
            data = await fetch_image_bytes(raw)
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("image fetch failed for %s: %s", raw, exc)
            return ""
        tmp = _write_temp_image(data, suffix=_suffix_for(raw))
        cleanup_temp = tmp
        path = tmp
        source_label = raw
    else:
        path = Path(raw)
        source_label = str(path)
        if not path.exists():
            logger.warning("image path does not exist: %s", path)
            return ""

    try:
        return await asyncio.get_running_loop().run_in_executor(
            None,
            _extract_sync,
            path,
            max_chars,
            conf_threshold,
            source_label,
            job,
        )
    finally:
        if cleanup_temp is not None:
            try:
                cleanup_temp.unlink()
            except OSError:
                pass


def extract_sync(
    path_or_url: str | Path,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    conf_threshold: float = DEFAULT_CONF_THRESHOLD,
    job: Job | None = None,
) -> str:
    """Blocking variant of :func:`extract` for the smoke verb / scripts."""
    raw = str(path_or_url)
    if _looks_like_url(raw):
        try:
            data = asyncio.run(fetch_image_bytes(raw))
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("image fetch failed for %s: %s", raw, exc)
            return ""
        tmp = _write_temp_image(data, suffix=_suffix_for(raw))
        try:
            return _extract_sync(tmp, max_chars, conf_threshold, raw, job)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    path = Path(raw)
    if not path.exists():
        logger.warning("image path does not exist: %s", path)
        return ""
    return _extract_sync(path, max_chars, conf_threshold, str(path), job)


def extract_from_bytes(
    data: bytes,
    *,
    suffix: str = _DEFAULT_IMAGE_SUFFIX,
    source_label: str = "<bytes>",
    max_chars: int = DEFAULT_MAX_CHARS,
    conf_threshold: float = DEFAULT_CONF_THRESHOLD,
    job: Job | None = None,
) -> str:
    """Run the layered pipeline against in-memory image bytes.

    Used by :mod:`web_fetch` when the upstream HTTP response was already
    an image — avoids re-downloading just to feed PIL/Tesseract.
    """
    if not data:
        return ""
    tmp = _write_temp_image(data, suffix=suffix)
    try:
        return _extract_sync(tmp, max_chars, conf_threshold, source_label, job)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


__all__ = [
    "DEFAULT_CONF_THRESHOLD",
    "DEFAULT_MAX_CHARS",
    "extract",
    "extract_from_bytes",
    "extract_sync",
    "fetch_image_bytes",
]
