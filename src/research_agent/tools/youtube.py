"""YouTube connector (issue #111): yt-dlp captions first, Whisper fallback.

Many investigative leads surface as YouTube content (interviews, conference
talks, podcast episodes, leaked recordings, news clips). The generic
trafilatura + Playwright path strips a YouTube watch page to its SPA shell
and yields nothing useful, so a dedicated connector is required.

Strategy:

1. **yt-dlp auto-captions first** — most YouTube videos have auto-generated
   English captions. ``yt-dlp --skip-download --write-auto-sub --write-sub
   --sub-format vtt --sub-langs 'en.*'`` is fast and free.
2. **Local Whisper fallback** — when no captions exist (older videos,
   intentional absence) we download the audio track with ``yt-dlp -x`` and
   route it through :func:`research_agent.tools.audio.transcribe`.

Public surface mirrors :mod:`research_agent.tools.reddit`:

* ``async def fetch(url) -> Source | None`` — accepts any YouTube URL shape
  (``youtube.com/watch?v=…``, ``youtu.be/…``, ``youtube.com/shorts/…``) and
  returns a :class:`Source` whose ``cleaned_text`` is the transcript with
  ``[mm:ss]`` timestamp markers preserved.
* ``async def search(query, max_results=25) -> list[SearchResult]`` — uses
  YouTube Data API v3 when ``YOUTUBE_API_KEY`` is set, otherwise scrapes the
  public results page via :func:`tools.browser.browser_session`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx

from research_agent import config
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

_YOUTUBE_HOSTS: frozenset[str] = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)

_DATA_API_BASE = "https://www.googleapis.com/youtube/v3/search"
_HTTP_TIMEOUT_S = 15.0
_YT_DLP_TIMEOUT_S = 120.0
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")

# yt-dlp surfaces age/region restrictions in stderr. Match conservatively —
# any of these substrings means we should bail rather than retry.
_RESTRICTED_KEYWORDS: tuple[str, ...] = (
    "Sign in to confirm your age",
    "age-restricted",
    "is not available in your country",
    "unavailable in your country",
    "video is private",
    "Private video",
    "Video unavailable",
)


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------


def _normalize_video_id(url: str) -> str | None:
    """Return the canonical ``https://www.youtube.com/watch?v=<id>`` URL.

    Handles ``watch?v=<id>``, ``youtu.be/<id>``, ``youtube.com/shorts/<id>``,
    and ``youtube.com/embed/<id>``. Returns None when the URL isn't a YouTube
    video reference (subscription pages, channel pages, malformed input).
    """
    if not url:
        return None
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host not in _YOUTUBE_HOSTS:
        return None

    video_id: str | None = None

    # youtu.be/<id>
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.lstrip("/").split("/")[0]
        if _VIDEO_ID_RE.match(candidate):
            video_id = candidate

    # youtube.com/watch?v=<id>
    if video_id is None and parsed.path.rstrip("/") == "/watch":
        params = parse_qs(parsed.query)
        candidate = (params.get("v") or [""])[0]
        if _VIDEO_ID_RE.match(candidate):
            video_id = candidate

    # youtube.com/shorts/<id>, youtube.com/embed/<id>, youtube.com/v/<id>
    if video_id is None:
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "v"}:
            candidate = parts[1]
            if _VIDEO_ID_RE.match(candidate):
                video_id = candidate

    if video_id is None:
        return None
    return f"https://www.youtube.com/watch?v={video_id}"


def _video_id_from_canonical(url: str) -> str | None:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    values = params.get("v")
    return values[0] if values else None


# ---------------------------------------------------------------------------
# yt-dlp subprocess
# ---------------------------------------------------------------------------


def _is_restricted(stderr: str) -> bool:
    if not stderr:
        return False
    for marker in _RESTRICTED_KEYWORDS:
        if marker.lower() in stderr.lower():
            return True
    return False


def _parse_metadata_line(line: str) -> dict[str, Any]:
    """Parse the ``--print`` line ``title\\tchannel\\tduration\\tupload_date``.

    Returns an empty dict when the line is malformed — callers fall back to
    the URL itself for the Source title.
    """
    if not line:
        return {}
    parts = line.split("\t")
    if len(parts) < 4:
        # Some videos elide channel or duration; pad with empties so the
        # zip below doesn't drop fields silently.
        parts = parts + [""] * (4 - len(parts))
    title, channel, duration_s, upload_date = parts[:4]
    meta: dict[str, Any] = {}
    if title and title != "NA":
        meta["title"] = title.strip()
    if channel and channel != "NA":
        meta["channel"] = channel.strip()
    try:
        if duration_s and duration_s != "NA":
            meta["duration_s"] = int(float(duration_s))
    except (TypeError, ValueError):
        pass
    if upload_date and upload_date != "NA" and len(upload_date) == 8:
        try:
            meta["upload_date"] = datetime.strptime(upload_date, "%Y%m%d").date().isoformat()
        except ValueError:
            pass
    return meta


async def _run_ytdlp_captions(
    url: str, tmpdir: Path
) -> tuple[dict[str, Any], Path | None, str]:
    """Run yt-dlp to write VTT captions + print metadata.

    Returns ``(metadata, vtt_path_or_None, stderr)``. ``vtt_path`` is None
    when no captions were emitted (yt-dlp prints the metadata regardless).
    """
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--write-auto-sub",
        "--write-sub",
        "--sub-format",
        "vtt",
        "--sub-langs",
        "en.*",
        "--print",
        "%(title)s\t%(channel)s\t%(duration)s\t%(upload_date)s",
        "-o",
        str(tmpdir / "%(id)s.%(ext)s"),
        url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=_YT_DLP_TIMEOUT_S
        )
    except FileNotFoundError as exc:
        logger.warning("yt-dlp not installed: %s", exc)
        return {}, None, "yt-dlp not installed"
    except TimeoutError as exc:
        logger.warning("yt-dlp captions timed out for %s: %s", url, exc)
        return {}, None, "timeout"

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")

    metadata: dict[str, Any] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # The metadata --print line is tab-separated; subtitle progress lines
        # printed to stdout look like ``[info] Writing video subtitles…``.
        if "\t" in line:
            metadata = _parse_metadata_line(line)
            break

    # Pick the first VTT file yt-dlp dropped in tmpdir.
    vtt_path: Path | None = None
    for candidate in sorted(tmpdir.glob("*.vtt")):
        vtt_path = candidate
        break

    return metadata, vtt_path, stderr


async def _run_ytdlp_audio(url: str, tmpdir: Path) -> Path | None:
    """Download bestaudio as an mp3 via yt-dlp; return the file path or None."""
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format",
        "mp3",
        "-o",
        str(tmpdir / "%(id)s.%(ext)s"),
        url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=_YT_DLP_TIMEOUT_S
        )
    except FileNotFoundError:
        return None
    except TimeoutError:
        logger.warning("yt-dlp audio download timed out for %s", url)
        return None

    if proc.returncode != 0:
        logger.warning(
            "yt-dlp audio download failed for %s: %s",
            url,
            stderr_b.decode("utf-8", errors="replace")[:200],
        )
        return None

    for candidate in sorted(tmpdir.glob("*.mp3")):
        return candidate
    return None


# ---------------------------------------------------------------------------
# VTT parsing
# ---------------------------------------------------------------------------


_VTT_TIMESTAMP_RE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2})\.\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}\.\d{3}"
)
_VTT_TAG_RE = re.compile(r"<[^>]+>")


def _format_mmss(hours: int, minutes: int, seconds: int) -> str:
    total_minutes = hours * 60 + minutes
    return f"[{total_minutes:02d}:{seconds:02d}]"


def _parse_vtt(text: str) -> str:
    """Convert VTT to a single transcript blob with ``[mm:ss]`` markers.

    YouTube auto-captions repeat the previous cue lines verbatim on the next
    cue ("rolling" captions). We dedupe consecutive identical caption lines
    so the transcript reads cleanly.
    """
    if not text:
        return ""

    lines: list[str] = []
    last_emitted: str | None = None
    current_marker: str | None = None
    pending_buffer: list[str] = []

    def _flush() -> None:
        nonlocal last_emitted, pending_buffer, current_marker
        if not pending_buffer:
            return
        joined = " ".join(s.strip() for s in pending_buffer if s.strip())
        joined = joined.strip()
        pending_buffer = []
        if not joined or joined == last_emitted:
            return
        if current_marker:
            lines.append(f"{current_marker} {joined}")
        else:
            lines.append(joined)
        last_emitted = joined

    for raw in text.splitlines():
        line = raw.rstrip()
        # Skip the WEBVTT header, NOTE blocks, STYLE blocks, and any
        # ``Kind:``/``Language:`` metadata that YouTube emits at the top.
        if not line:
            _flush()
            continue
        if line.startswith("WEBVTT"):
            continue
        if line.startswith(("NOTE", "STYLE", "Kind:", "Language:")):
            continue
        m = _VTT_TIMESTAMP_RE.match(line)
        if m:
            _flush()
            hours = int(m.group(1))
            minutes = int(m.group(2))
            seconds = int(m.group(3))
            current_marker = _format_mmss(hours, minutes, seconds)
            continue
        # Cue identifier (just digits or a hash) — skip.
        if line.isdigit():
            continue
        # Strip inline timing tags like <c> and <00:00:00.000>.
        cleaned = _VTT_TAG_RE.sub("", line).strip()
        if not cleaned:
            continue
        pending_buffer.append(cleaned)

    _flush()
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Public: fetch
# ---------------------------------------------------------------------------


async def fetch(url: str) -> Source | None:
    """Fetch the transcript for a YouTube URL.

    Pipeline: yt-dlp → VTT (captions). When captions are absent yt-dlp
    re-runs to pull the audio track and we route it through
    :func:`audio.transcribe`. Age-restricted / region-locked / removed
    videos return None with a WARN log so the planner can move on rather
    than retry forever.
    """
    canonical = _normalize_video_id(url)
    if canonical is None:
        return None

    tmp_root = Path(tempfile.mkdtemp(prefix="yt_dlp_"))
    try:
        metadata, vtt_path, stderr = await _run_ytdlp_captions(canonical, tmp_root)

        if _is_restricted(stderr):
            logger.warning(
                "youtube fetch skipped (age-restricted / region-locked): %s", canonical
            )
            return None

        title = metadata.get("title") or canonical
        channel = metadata.get("channel")
        duration_s = metadata.get("duration_s")
        upload_date = metadata.get("upload_date")

        base_metadata: dict[str, Any] = {
            "platform": "youtube",
            "channel": channel,
            "duration_s": duration_s,
            "upload_date": upload_date,
            "video_id": _video_id_from_canonical(canonical),
            "transcript_unavailable": False,
        }

        # Captions path.
        if vtt_path is not None and vtt_path.exists():
            try:
                vtt_text = vtt_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.warning("youtube vtt read failed for %s: %s", canonical, exc)
                vtt_text = ""
            transcript = _parse_vtt(vtt_text)
            if transcript:
                meta = dict(base_metadata)
                meta["fetched_via"] = "yt-dlp-captions"
                return Source(
                    url=canonical,
                    title=title,
                    cleaned_text=transcript,
                    fetched_at=datetime.now(UTC),
                    source_kind="web",
                    metadata=meta,
                )

        # Whisper fallback path.
        audio_path = await _run_ytdlp_audio(canonical, tmp_root)
        if audio_path is not None and audio_path.exists():
            from research_agent.tools import audio as audio_tool

            try:
                transcript = await audio_tool.transcribe(audio_path)
            except Exception as exc:  # noqa: BLE001 — never crash the loop
                logger.warning(
                    "youtube whisper fallback failed for %s: %s", canonical, exc
                )
                transcript = ""
            if transcript.strip():
                meta = dict(base_metadata)
                meta["fetched_via"] = "yt-dlp-whisper"
                return Source(
                    url=canonical,
                    title=title,
                    cleaned_text=transcript,
                    fetched_at=datetime.now(UTC),
                    source_kind="web",
                    metadata=meta,
                )

        # Both paths empty — only return a stub Source if we actually got
        # metadata back (means yt-dlp reached the video). Otherwise the call
        # failed before it touched YouTube and we should signal None so the
        # planner doesn't waste a citation slot on an empty body.
        if metadata:
            meta = dict(base_metadata)
            meta["fetched_via"] = "yt-dlp"
            meta["transcript_unavailable"] = True
            return Source(
                url=canonical,
                title=title,
                cleaned_text="",
                fetched_at=datetime.now(UTC),
                source_kind="web",
                metadata=meta,
            )
        return None
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Public: search
# ---------------------------------------------------------------------------


def _parse_data_api_results(payload: dict[str, Any]) -> list[SearchResult]:
    items = payload.get("items") or []
    out: list[SearchResult] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        snippet = (item.get("snippet") or {}) if isinstance(item.get("snippet"), dict) else {}
        ident = (item.get("id") or {}) if isinstance(item.get("id"), dict) else {}
        video_id = ident.get("videoId")
        title = (snippet.get("title") or "").strip()
        if not video_id or not title:
            continue
        channel = (snippet.get("channelTitle") or "").strip()
        description = (snippet.get("description") or "").strip()
        published_raw = snippet.get("publishedAt")
        published_at: datetime | None = None
        if isinstance(published_raw, str) and published_raw:
            try:
                published_at = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
            except ValueError:
                published_at = None
        out.append(
            SearchResult(
                url=f"https://www.youtube.com/watch?v={video_id}",
                title=title,
                snippet=description[:400],
                published_at=published_at,
                source_kind="web",
                extras={
                    "channel": channel,
                    "video_id": video_id,
                    "fetched_via": "youtube-data-api",
                },
            )
        )
    return out


async def _search_via_data_api(query: str, max_results: int, api_key: str) -> list[SearchResult]:
    params: dict[str, str | int] = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": max(1, min(int(max_results), 50)),
        "key": api_key,
    }
    try:
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT_S, follow_redirects=True
        ) as client:
            resp = await client.get(_DATA_API_BASE, params=params)
    except httpx.HTTPError as exc:
        logger.warning("youtube data api HTTP error for %r: %s", query, exc)
        return []
    if resp.status_code != 200:
        logger.warning(
            "youtube data api returned %s for %r: %s",
            resp.status_code,
            query,
            resp.text[:200],
        )
        return []
    try:
        payload = resp.json()
    except ValueError as exc:
        logger.warning("youtube data api JSON decode failed: %s", exc)
        return []
    return _parse_data_api_results(payload)[:max_results]


_YT_INITIAL_DATA_RE = re.compile(
    r"var\s+ytInitialData\s*=\s*(\{.*?\})\s*;\s*</script>", re.DOTALL
)


def _walk_video_renderers(node: Any) -> list[dict[str, Any]]:
    """Recursively pull every ``videoRenderer`` dict out of ``ytInitialData``.

    YouTube's SERP shape changes; rather than encode the exact path through
    ``contents.twoColumnSearchResultsRenderer.…`` we walk the whole tree so
    the parser survives minor reshufflings.
    """
    out: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if "videoRenderer" in node and isinstance(node["videoRenderer"], dict):
            out.append(node["videoRenderer"])
        for value in node.values():
            out.extend(_walk_video_renderers(value))
    elif isinstance(node, list):
        for item in node:
            out.extend(_walk_video_renderers(item))
    return out


def _runs_to_text(runs: Any) -> str:
    if not isinstance(runs, list):
        return ""
    return "".join(
        r.get("text", "") for r in runs if isinstance(r, dict) and isinstance(r.get("text"), str)
    ).strip()


def _renderer_to_search_result(renderer: dict[str, Any]) -> SearchResult | None:
    video_id = renderer.get("videoId")
    if not isinstance(video_id, str) or not video_id:
        return None
    title_obj = renderer.get("title") or {}
    title = ""
    if isinstance(title_obj, dict):
        title = _runs_to_text(title_obj.get("runs"))
        if not title:
            simple = title_obj.get("simpleText")
            if isinstance(simple, str):
                title = simple.strip()
    if not title:
        return None

    channel = ""
    owner = renderer.get("ownerText") or renderer.get("longBylineText") or {}
    if isinstance(owner, dict):
        channel = _runs_to_text(owner.get("runs"))

    snippet = ""
    desc = renderer.get("detailedMetadataSnippets")
    if isinstance(desc, list) and desc:
        first = desc[0]
        if isinstance(first, dict):
            snippet_obj = first.get("snippetText") or {}
            if isinstance(snippet_obj, dict):
                snippet = _runs_to_text(snippet_obj.get("runs"))

    published = ""
    published_obj = renderer.get("publishedTimeText") or {}
    if isinstance(published_obj, dict):
        published = published_obj.get("simpleText") or ""

    view_count = ""
    view_obj = renderer.get("viewCountText") or {}
    if isinstance(view_obj, dict):
        view_count = view_obj.get("simpleText") or _runs_to_text(view_obj.get("runs"))

    return SearchResult(
        url=f"https://www.youtube.com/watch?v={video_id}",
        title=title,
        snippet=snippet[:400],
        published_at=None,
        source_kind="web",
        extras={
            "channel": channel,
            "video_id": video_id,
            "view_count_text": view_count or None,
            "published_text": published or None,
            "fetched_via": "youtube-serp",
        },
    )


async def _search_via_serp(query: str, max_results: int) -> list[SearchResult]:
    """Scrape the public YouTube results page when no API key is configured."""
    from research_agent.tools import browser

    target = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    try:
        async with browser.browser_session() as ctx:
            page = await ctx.new_page()
            try:
                await browser.navigate(page, target)
                html = await page.content()
            finally:
                await page.close()
    except Exception as exc:  # noqa: BLE001 — never crash the loop
        logger.warning("youtube serp scrape failed for %r: %s", query, exc)
        return []

    match = _YT_INITIAL_DATA_RE.search(html)
    if match is None:
        logger.warning("youtube serp scrape: ytInitialData not found for %r", query)
        return []
    try:
        data = json.loads(match.group(1))
    except ValueError as exc:
        logger.warning("youtube serp scrape JSON decode failed: %s", exc)
        return []

    renderers = _walk_video_renderers(data)
    out: list[SearchResult] = []
    for renderer in renderers:
        sr = _renderer_to_search_result(renderer)
        if sr is not None:
            out.append(sr)
        if len(out) >= max_results:
            break
    return out


async def search(query: str, max_results: int = 25) -> list[SearchResult]:
    """Search YouTube; return up to ``max_results`` :class:`SearchResult`.

    Uses the YouTube Data API v3 when ``YOUTUBE_API_KEY`` is configured,
    otherwise scrapes ``youtube.com/results`` via the shared Playwright
    session. Returns ``[]`` on any error rather than raising — search
    failures should not abort the orchestration loop.
    """
    if not query or not query.strip():
        return []

    api_key = config.get("YOUTUBE_API_KEY")
    if api_key:
        return await _search_via_data_api(query, max_results, api_key)
    return await _search_via_serp(query, max_results)


__all__ = ["fetch", "search"]
