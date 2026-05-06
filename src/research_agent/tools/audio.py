"""Local audio transcription via Whisper (issue #110).

Use case: podcast episodes, recorded interviews, leaked audio, conference
talks, court audio. The OpenAI hosted endpoint runs $0.006/min — cheap per
file, but ambient monitoring of dozens of feeds adds up. This module keeps
transcription fully local.

Backend selection (lazy import, first one available wins):

1. ``mlx_whisper`` — Apple Silicon native (Metal). Faster-than-realtime on
   M-series; default on ``arm64`` Darwin.
2. ``pywhispercpp`` — cross-platform whisper.cpp Python binding; CPU-fine
   on Linux/Intel.
3. ``openai-whisper`` (the original PyTorch package) — last-resort fallback
   so the module remains importable in environments where the faster
   bindings are unavailable.

When none are installed we log a warning and return an empty string so the
agent can degrade gracefully rather than crash.

Model size / latency trade-off (informational — `model="base"` is the
default):

| Model    | Speed (M-series, MLX) | Quality        | Best for                 |
|----------|-----------------------|----------------|--------------------------|
| ``tiny`` | ~10x realtime         | low (drops)    | quick triage / smoke     |
| ``base`` | ~6x realtime          | good (default) | general-purpose          |
| ``small``| ~3x realtime          | better         | named-entity-heavy audio |
| ``medium``| ~1.5x realtime       | best           | accented / noisy audio   |

Long files are sliced at 30-minute boundaries to keep peak memory bounded
regardless of clip length. Speaker diarization is best-effort: backends
that don't expose speaker labels get a flat ``[Speaker]`` prefix per line.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

from research_agent import config
from research_agent.storage.jobs import Job

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "base"

SUPPORTED_AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp3", ".m4a", ".wav", ".ogg", ".flac"}
)

AUDIO_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "audio/mpeg",
        "audio/mp3",
        "audio/mp4",
        "audio/x-m4a",
        "audio/wav",
        "audio/x-wav",
        "audio/ogg",
        "audio/flac",
        "audio/x-flac",
    }
)

# 30 minutes per chunk keeps memory bounded on multi-hour podcasts. The
# value is chosen so a single chunk fits comfortably in MLX/whisper.cpp's
# default context window without forcing them to internally re-window.
CHUNK_SECONDS = 1800

_USER_AGENT_DEFAULT = "research-agent/0.1"
_FETCH_TIMEOUT_S = 120.0


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _user_agent() -> str:
    return config.get("RESEARCH_USER_AGENT") or _USER_AGENT_DEFAULT


def _looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


# ---------------------------------------------------------------------------
# Fetch + temp-file helpers
# ---------------------------------------------------------------------------


def _suffix_for(url: str) -> str:
    """Pick a temp-file suffix from the URL path so ffmpeg can sniff it.

    Falls back to ``.mp3`` because Whisper backends accept anything ffmpeg
    can decode and ``.mp3`` is the modal format on the open web.
    """
    lowered = url.lower()
    for ext in SUPPORTED_AUDIO_EXTENSIONS:
        if lowered.endswith(ext) or f"{ext}?" in lowered:
            return ext
    return ".mp3"


def _write_temp_audio(data: bytes, suffix: str = ".mp3") -> Path:
    """Write ``data`` to a temp audio file; close the OS fd to avoid a leak."""
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return Path(tmp_path)


async def fetch_audio_bytes(url: str, *, timeout: float = _FETCH_TIMEOUT_S) -> bytes:
    """Download an audio file over HTTP(S) and return the raw bytes.

    Raises :class:`httpx.HTTPError` on transport failure so callers can
    decide whether to retry.
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
# ffmpeg-driven chunking
# ---------------------------------------------------------------------------


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _chunk_audio(path: Path, *, chunk_seconds: int = CHUNK_SECONDS) -> list[Path]:
    """Slice ``path`` into ``chunk_seconds``-bounded WAV chunks via ffmpeg.

    Returns ``[path]`` (the original) when ffmpeg is unavailable — Whisper
    backends can usually handle the full file; the chunk pass is a memory
    safeguard, not a correctness requirement. Chunks are 16-bit PCM mono
    at 16 kHz which is what every Whisper variant expects internally, so
    the backend doesn't have to re-resample.
    """
    if not _ffmpeg_available():
        logger.warning("ffmpeg not found on PATH — transcribing as a single chunk")
        return [path]

    out_dir = Path(tempfile.mkdtemp(prefix="audio_chunks_"))
    pattern = out_dir / "chunk_%03d.wav"

    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-f",
        "segment",
        "-segment_time",
        str(chunk_seconds),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(pattern),
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.warning("ffmpeg chunking failed: %s — falling back to whole file", exc)
        shutil.rmtree(out_dir, ignore_errors=True)
        return [path]

    chunks = sorted(out_dir.glob("chunk_*.wav"))
    if not chunks:
        # ffmpeg succeeded but produced nothing (zero-length input?) — clean up.
        shutil.rmtree(out_dir, ignore_errors=True)
        return [path]
    return chunks


def _cleanup_chunks(chunks: list[Path], original: Path) -> None:
    """Remove chunk files (and their parent temp dir) without touching the original."""
    for chunk in chunks:
        if chunk == original:
            continue
        try:
            chunk.unlink()
        except OSError:
            pass
    # Try to remove the parent directory if it's our temp dir.
    parents = {c.parent for c in chunks if c != original}
    for parent in parents:
        if parent.name.startswith("audio_chunks_"):
            shutil.rmtree(parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Backend dispatch
# ---------------------------------------------------------------------------


def _segments_to_lines(segments: list[dict[str, Any]]) -> list[str]:
    """Render whisper-style segment dicts as ``[Speaker] text`` lines.

    Speaker diarization is best-effort. Backends that expose a ``speaker``
    field (e.g. mlx_whisper with ``word_timestamps=True`` and a downstream
    diarizer plugged in) get a per-line ``[Speaker N]``. Otherwise we tag
    every line ``[Speaker]`` so the output shape is stable and downstream
    citation/synthesis code can rely on the prefix.
    """
    lines: list[str] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        speaker = seg.get("speaker")
        if speaker:
            lines.append(f"[Speaker {speaker}] {text}")
        else:
            lines.append(f"[Speaker] {text}")
    return lines


def _transcribe_with_mlx(path: Path, model: str) -> list[dict[str, Any]] | None:
    try:
        import mlx_whisper  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 — fall through to the next backend
        logger.debug("mlx_whisper unavailable: %s", exc)
        return None
    try:
        result = mlx_whisper.transcribe(
            str(path),
            path_or_hf_repo=f"mlx-community/whisper-{model}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("mlx_whisper transcribe failed for %s: %s", path, exc)
        return None
    return list(result.get("segments") or [])


def _transcribe_with_pywhispercpp(path: Path, model: str) -> list[dict[str, Any]] | None:
    try:
        from pywhispercpp.model import Model  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        logger.debug("pywhispercpp unavailable: %s", exc)
        return None
    try:
        m = Model(model)
        segments = m.transcribe(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("pywhispercpp transcribe failed for %s: %s", path, exc)
        return None
    out: list[dict[str, Any]] = []
    for seg in segments:
        out.append({"text": getattr(seg, "text", "") or "", "speaker": None})
    return out


def _transcribe_with_openai_whisper(path: Path, model: str) -> list[dict[str, Any]] | None:
    try:
        import whisper  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        logger.debug("openai-whisper unavailable: %s", exc)
        return None
    try:
        m = whisper.load_model(model)
        result = m.transcribe(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("openai-whisper transcribe failed for %s: %s", path, exc)
        return None
    return list(result.get("segments") or [])


def _transcribe_chunk(path: Path, model: str) -> list[str]:
    """Run the first available Whisper backend; return formatted lines.

    Backend order is *resolved per call* rather than at import time so a
    test can monkey-patch ``_transcribe_with_mlx`` etc. on the module.
    Returns an empty list when no backend is importable — the caller emits
    a markdown stub for that chunk so the operator sees the gap.
    """
    backends: list[Any]
    if _is_apple_silicon():
        backends = [
            _transcribe_with_mlx,
            _transcribe_with_pywhispercpp,
            _transcribe_with_openai_whisper,
        ]
    else:
        backends = [
            _transcribe_with_pywhispercpp,
            _transcribe_with_openai_whisper,
        ]

    for backend in backends:
        segments = backend(path, model)
        if segments is None:
            continue
        return _segments_to_lines(segments)

    logger.warning(
        "no whisper backend available — install mlx-whisper, pywhispercpp, or openai-whisper",
    )
    return []


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _format_seconds(seconds: int) -> str:
    minutes, sec = divmod(seconds, 60)
    return f"{minutes:02d}:{sec:02d}"


def _render_markdown(chunk_lines: list[list[str]], *, chunk_seconds: int) -> str:
    """Stitch per-chunk lines into a single markdown transcript.

    ``## Chunk N (mm:ss-mm:ss)`` headings give the synthesizer a coarse
    timestamp it can cite without us having to thread per-segment start
    offsets through the whole pipeline.
    """
    parts: list[str] = []
    for idx, lines in enumerate(chunk_lines):
        start = idx * chunk_seconds
        end = start + chunk_seconds
        heading = f"## Chunk {idx + 1} ({_format_seconds(start)}-{_format_seconds(end)})"
        body = "\n".join(lines) if lines else "[Speaker] (no transcript produced for this chunk)"
        parts.append(f"{heading}\n\n{body}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def _transcribe_path_sync(
    path: Path,
    model: str,
    chunk_seconds: int,
) -> str:
    """Chunk + transcribe + render. Pulled out so async + sync share a body.

    Positional-only so :func:`asyncio.AbstractEventLoop.run_in_executor` (which
    can't pass kwargs) can call it directly.
    """
    chunks = _chunk_audio(path, chunk_seconds=chunk_seconds)
    chunk_lines: list[list[str]] = []
    try:
        for chunk in chunks:
            chunk_lines.append(_transcribe_chunk(chunk, model))
    finally:
        _cleanup_chunks(chunks, path)
    return _render_markdown(chunk_lines, chunk_seconds=chunk_seconds)


async def transcribe(
    path_or_url: str | Path,
    *,
    model: str = DEFAULT_MODEL,
    chunk_seconds: int = CHUNK_SECONDS,
    job: Job | None = None,  # noqa: ARG001 — reserved for parity with pdf.extract
) -> str:
    """Transcribe a local audio file or remote URL into markdown.

    Returns markdown of the form:

        ## Chunk 1 (00:00-30:00)

        [Speaker] hello world
        [Speaker 2] response

    Returns the empty string when no whisper backend is installed or the
    file is unreachable. Callers decide whether to record a :class:`Source`
    or skip the document.
    """
    raw = str(path_or_url)
    cleanup_temp: Path | None = None

    if _looks_like_url(raw):
        try:
            data = await fetch_audio_bytes(raw)
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("audio fetch failed for %s: %s", raw, exc)
            return ""
        tmp = _write_temp_audio(data, suffix=_suffix_for(raw))
        cleanup_temp = tmp
        path = tmp
    else:
        path = Path(raw)
        if not path.exists():
            logger.warning("audio path does not exist: %s", path)
            return ""

    try:
        return await asyncio.get_running_loop().run_in_executor(
            None,
            _transcribe_path_sync,
            path,
            model,
            chunk_seconds,
        )
    finally:
        if cleanup_temp is not None:
            try:
                cleanup_temp.unlink()
            except OSError:
                pass


def transcribe_sync(
    path_or_url: str | Path,
    *,
    model: str = DEFAULT_MODEL,
    chunk_seconds: int = CHUNK_SECONDS,
    job: Job | None = None,  # noqa: ARG001
) -> str:
    """Blocking variant of :func:`transcribe` for the smoke verb / scripts."""
    raw = str(path_or_url)
    if _looks_like_url(raw):
        try:
            data = asyncio.run(fetch_audio_bytes(raw))
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("audio fetch failed for %s: %s", raw, exc)
            return ""
        tmp = _write_temp_audio(data, suffix=_suffix_for(raw))
        try:
            return _transcribe_path_sync(tmp, model, chunk_seconds)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    path = Path(raw)
    if not path.exists():
        logger.warning("audio path does not exist: %s", path)
        return ""
    return _transcribe_path_sync(path, model, chunk_seconds)


def transcribe_from_bytes(
    data: bytes,
    *,
    suffix: str = ".mp3",
    model: str = DEFAULT_MODEL,
    chunk_seconds: int = CHUNK_SECONDS,
    job: Job | None = None,  # noqa: ARG001
) -> str:
    """Transcribe raw bytes already in memory.

    Used by :mod:`web_fetch` when the upstream HTTP response declared an
    audio content-type — avoids a redundant download just to feed the file
    into Whisper.
    """
    if not data:
        return ""
    tmp = _write_temp_audio(data, suffix=suffix)
    try:
        return _transcribe_path_sync(tmp, model, chunk_seconds)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


__all__ = [
    "AUDIO_CONTENT_TYPES",
    "CHUNK_SECONDS",
    "DEFAULT_MODEL",
    "SUPPORTED_AUDIO_EXTENSIONS",
    "fetch_audio_bytes",
    "transcribe",
    "transcribe_from_bytes",
    "transcribe_sync",
]
