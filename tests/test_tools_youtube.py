"""Tests for `research_agent.tools.youtube` (issue #111)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from research_agent import config as config_module
from research_agent.tools import web_fetch, youtube


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.delenv("RESEARCH_IGNORE_ROBOTS", raising=False)
    web_fetch.reset_for_tests()
    config_module.reset_for_tests()
    yield
    web_fetch.reset_for_tests()
    config_module.reset_for_tests()


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------


def test_normalize_video_id_watch_url():
    out = youtube._normalize_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert out == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_normalize_video_id_youtu_be_short():
    out = youtube._normalize_video_id("https://youtu.be/dQw4w9WgXcQ?t=42")
    assert out == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_normalize_video_id_shorts():
    out = youtube._normalize_video_id("https://www.youtube.com/shorts/abc123XYZ_-")
    assert out == "https://www.youtube.com/watch?v=abc123XYZ_-"


def test_normalize_video_id_embed():
    out = youtube._normalize_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ")
    assert out == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_normalize_video_id_m_subdomain():
    out = youtube._normalize_video_id("https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=foo")
    assert out == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_normalize_video_id_rejects_non_youtube():
    assert youtube._normalize_video_id("https://example.com/watch?v=abc") is None


def test_normalize_video_id_rejects_channel_pages():
    assert youtube._normalize_video_id("https://www.youtube.com/@SomeChannel") is None


def test_normalize_video_id_rejects_results_page():
    assert youtube._normalize_video_id("https://www.youtube.com/results?search_query=foo") is None


# ---------------------------------------------------------------------------
# VTT parser
# ---------------------------------------------------------------------------


def test_parse_vtt_strips_header_and_emits_timestamps():
    vtt = (
        "WEBVTT\n"
        "Kind: captions\n"
        "Language: en\n"
        "\n"
        "00:00:00.000 --> 00:00:04.000\n"
        "hello world\n"
        "\n"
        "00:00:04.000 --> 00:00:08.000\n"
        "second cue\n"
    )
    out = youtube._parse_vtt(vtt)
    assert "WEBVTT" not in out
    assert "Kind:" not in out
    assert "[00:00] hello world" in out
    assert "[00:04] second cue" in out


def test_parse_vtt_dedupes_consecutive_repeats():
    """YouTube auto-captions emit rolling cues — the same line on each cue.

    The parser must dedupe consecutive duplicates so the transcript reads
    cleanly rather than printing every phrase three times.
    """
    vtt = (
        "WEBVTT\n"
        "\n"
        "00:00:00.000 --> 00:00:02.000\n"
        "hello world\n"
        "\n"
        "00:00:02.000 --> 00:00:04.000\n"
        "hello world\n"
        "\n"
        "00:00:04.000 --> 00:00:06.000\n"
        "hello world\n"
    )
    out = youtube._parse_vtt(vtt)
    assert out.count("hello world") == 1


def test_parse_vtt_strips_inline_tags():
    vtt = (
        "WEBVTT\n"
        "\n"
        "00:00:00.000 --> 00:00:04.000\n"
        "<c>tagged</c> text <00:00:01.000><c>here</c>\n"
    )
    out = youtube._parse_vtt(vtt)
    assert "<c>" not in out
    assert "tagged text here" in out


def test_parse_vtt_handles_hours():
    vtt = (
        "WEBVTT\n"
        "\n"
        "01:02:03.000 --> 01:02:07.000\n"
        "deep in a long video\n"
    )
    out = youtube._parse_vtt(vtt)
    # 1h 2m → 62 minutes total in the [mm:ss] marker.
    assert "[62:03] deep in a long video" in out


# ---------------------------------------------------------------------------
# Metadata + restriction parsing
# ---------------------------------------------------------------------------


def test_parse_metadata_line_full():
    meta = youtube._parse_metadata_line("My Video\tACME News\t1234\t20240115")
    assert meta == {
        "title": "My Video",
        "channel": "ACME News",
        "duration_s": 1234,
        "upload_date": "2024-01-15",
    }


def test_parse_metadata_line_handles_na_fields():
    meta = youtube._parse_metadata_line("Title\tNA\tNA\tNA")
    assert meta == {"title": "Title"}


def test_is_restricted_detects_age_gate():
    assert youtube._is_restricted("ERROR: Sign in to confirm your age") is True
    assert youtube._is_restricted("ERROR: Video unavailable") is True
    assert youtube._is_restricted("[info] Writing video subtitles…") is False
    assert youtube._is_restricted("") is False


# ---------------------------------------------------------------------------
# fetch() — captions path
# ---------------------------------------------------------------------------


_FAKE_VTT = (
    "WEBVTT\n"
    "Kind: captions\n"
    "\n"
    "00:00:00.000 --> 00:00:04.000\n"
    "george santos addressed the house\n"
    "\n"
    "00:00:04.000 --> 00:00:08.000\n"
    "in a remarkable speech\n"
)


async def test_fetch_returns_source_with_transcript(monkeypatch, tmp_path):
    captured: dict[str, Any] = {}

    async def _fake_captions(url, tmpdir):
        captured["url"] = url
        captured["tmpdir"] = tmpdir
        # Plant a VTT file the way yt-dlp would.
        vtt = tmpdir / "abc123.en.vtt"
        vtt.write_text(_FAKE_VTT, encoding="utf-8")
        meta = {
            "title": "Santos floor speech",
            "channel": "C-SPAN",
            "duration_s": 600,
            "upload_date": "2024-01-15",
        }
        return meta, vtt, ""

    async def _no_audio(*args, **kwargs):
        raise AssertionError("audio fallback should not run when captions exist")

    monkeypatch.setattr(youtube, "_run_ytdlp_captions", _fake_captions)
    monkeypatch.setattr(youtube, "_run_ytdlp_audio", _no_audio)

    src = await youtube.fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert src is not None
    assert src.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert src.title == "Santos floor speech"
    assert src.source_kind == "web"
    assert src.metadata["fetched_via"] == "yt-dlp-captions"
    assert src.metadata["channel"] == "C-SPAN"
    assert src.metadata["duration_s"] == 600
    assert src.metadata["transcript_unavailable"] is False
    assert src.metadata["video_id"] == "dQw4w9WgXcQ"
    assert "[00:00] george santos" in src.cleaned_text
    assert "[00:04] in a remarkable speech" in src.cleaned_text
    # tmpdir is cleaned up after fetch returns.
    assert not captured["tmpdir"].exists()


async def test_fetch_falls_back_to_whisper(monkeypatch, tmp_path):
    """When yt-dlp produces no VTT, audio.transcribe is used instead."""

    async def _fake_captions(url, tmpdir):
        # No VTT planted — captions absent.
        return {"title": "Older interview", "channel": "OldUploader"}, None, ""

    async def _fake_audio(url, tmpdir):
        mp3 = tmpdir / "abc123.mp3"
        mp3.write_bytes(b"\x00\x01\x02")
        return mp3

    transcribed_paths: list[Path] = []

    async def _fake_transcribe(path, **kwargs):
        transcribed_paths.append(Path(path))
        return "## Chunk 1 (00:00-30:00)\n\n[Speaker] this is the transcribed audio body."

    monkeypatch.setattr(youtube, "_run_ytdlp_captions", _fake_captions)
    monkeypatch.setattr(youtube, "_run_ytdlp_audio", _fake_audio)

    from research_agent.tools import audio

    monkeypatch.setattr(audio, "transcribe", _fake_transcribe)

    src = await youtube.fetch("https://youtu.be/dQw4w9WgXcQ")
    assert src is not None
    assert src.metadata["fetched_via"] == "yt-dlp-whisper"
    assert src.metadata["transcript_unavailable"] is False
    assert "[Speaker] this is the transcribed audio body" in src.cleaned_text
    assert len(transcribed_paths) == 1


async def test_fetch_returns_none_for_age_restricted(monkeypatch):
    async def _fake_captions(url, tmpdir):
        stderr = "ERROR: Sign in to confirm your age. This video may be inappropriate."
        return {}, None, stderr

    async def _no_audio(*args, **kwargs):
        raise AssertionError("audio fallback should not run for restricted videos")

    monkeypatch.setattr(youtube, "_run_ytdlp_captions", _fake_captions)
    monkeypatch.setattr(youtube, "_run_ytdlp_audio", _no_audio)

    src = await youtube.fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert src is None


async def test_fetch_returns_stub_when_metadata_only(monkeypatch):
    """Metadata succeeds, captions and audio both fail → stub Source."""

    async def _fake_captions(url, tmpdir):
        return {"title": "ghost video", "channel": "Ghost"}, None, ""

    async def _fake_audio(url, tmpdir):
        return None

    monkeypatch.setattr(youtube, "_run_ytdlp_captions", _fake_captions)
    monkeypatch.setattr(youtube, "_run_ytdlp_audio", _fake_audio)

    src = await youtube.fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert src is not None
    assert src.cleaned_text == ""
    assert src.metadata["transcript_unavailable"] is True
    assert src.metadata["fetched_via"] == "yt-dlp"


async def test_fetch_returns_none_when_no_metadata(monkeypatch):
    """If yt-dlp can't even surface metadata we return None — not a stub.

    Burning a citation slot on a Source with no body and no metadata is
    worse than letting the planner re-route.
    """

    async def _fake_captions(url, tmpdir):
        return {}, None, "ERROR: yt-dlp not installed"

    async def _fake_audio(url, tmpdir):
        return None

    monkeypatch.setattr(youtube, "_run_ytdlp_captions", _fake_captions)
    monkeypatch.setattr(youtube, "_run_ytdlp_audio", _fake_audio)

    src = await youtube.fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert src is None


async def test_fetch_returns_none_for_non_youtube_url(monkeypatch):
    sentinel: dict[str, bool] = {"called": False}

    async def _fake_captions(url, tmpdir):
        sentinel["called"] = True
        return {}, None, ""

    monkeypatch.setattr(youtube, "_run_ytdlp_captions", _fake_captions)
    src = await youtube.fetch("https://example.com/")
    assert src is None
    assert sentinel["called"] is False


# ---------------------------------------------------------------------------
# search() — Data API path
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _StubAsyncClient:
    last_url: str | None = None
    last_params: dict[str, Any] | None = None

    def __init__(
        self,
        *,
        response: _StubResponse | None = None,
        raise_with: Exception | None = None,
    ) -> None:
        self._response = response
        self._raise = raise_with

    async def __aenter__(self) -> _StubAsyncClient:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def get(self, url: str, params: dict[str, Any] | None = None, **_: Any) -> _StubResponse:
        type(self).last_url = url
        type(self).last_params = dict(params or {})
        if self._raise is not None:
            raise self._raise
        assert self._response is not None
        return self._response


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> type[_StubAsyncClient]:
    stub_cls = type("_Stub", (_StubAsyncClient,), {})
    stub_cls.last_url = None
    stub_cls.last_params = None
    instance = stub_cls(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: instance)
    return stub_cls


async def test_search_uses_data_api_when_key_set(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaTest")
    config_module.reset_for_tests()

    payload = {
        "items": [
            {
                "id": {"videoId": "vid001"},
                "snippet": {
                    "title": "Santos hearing live",
                    "channelTitle": "ABC News",
                    "description": "Coverage of the floor speech.",
                    "publishedAt": "2024-01-15T12:00:00Z",
                },
            },
            {
                "id": {"videoId": "vid002"},
                "snippet": {
                    "title": "Santos commentary",
                    "channelTitle": "Pundit Channel",
                    "description": "Reaction.",
                    "publishedAt": "2024-01-16T08:30:00Z",
                },
            },
        ]
    }
    stub = _patch_httpx(monkeypatch, response=_StubResponse(200, payload))

    results = await youtube.search("george santos", max_results=5)
    assert stub.last_url == youtube._DATA_API_BASE
    assert stub.last_params["q"] == "george santos"
    assert stub.last_params["key"] == "AIzaTest"
    assert stub.last_params["part"] == "snippet"
    assert stub.last_params["type"] == "video"
    assert stub.last_params["maxResults"] == 5
    assert len(results) == 2
    assert results[0].url == "https://www.youtube.com/watch?v=vid001"
    assert results[0].title == "Santos hearing live"
    assert results[0].extras["channel"] == "ABC News"
    assert results[0].extras["fetched_via"] == "youtube-data-api"
    assert results[0].published_at is not None
    assert results[0].published_at.year == 2024


async def test_search_data_api_clamps_max_results(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaTest")
    config_module.reset_for_tests()
    stub = _patch_httpx(monkeypatch, response=_StubResponse(200, {"items": []}))
    await youtube.search("anything", max_results=999)
    # Data API caps at 50 per page.
    assert stub.last_params["maxResults"] == 50


async def test_search_data_api_http_error_returns_empty(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaTest")
    config_module.reset_for_tests()
    _patch_httpx(monkeypatch, raise_with=httpx.ConnectError("boom"))
    assert await youtube.search("anything") == []


async def test_search_data_api_non_200_returns_empty(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaTest")
    config_module.reset_for_tests()
    _patch_httpx(monkeypatch, response=_StubResponse(403, {"error": "quota"}))
    assert await youtube.search("anything") == []


async def test_search_empty_query_returns_immediately():
    # Even with no API key set, an empty query short-circuits before any I/O.
    assert await youtube.search("   ") == []


# ---------------------------------------------------------------------------
# search() — SERP fallback
# ---------------------------------------------------------------------------


def _build_serp_html(*videos: dict[str, str]) -> str:
    """Construct a minimal results page that embeds ``ytInitialData``."""
    contents = []
    for v in videos:
        contents.append(
            {
                "videoRenderer": {
                    "videoId": v["video_id"],
                    "title": {"runs": [{"text": v["title"]}]},
                    "ownerText": {"runs": [{"text": v["channel"]}]},
                    "viewCountText": {"simpleText": v.get("views", "1,234 views")},
                    "publishedTimeText": {"simpleText": v.get("published", "1 day ago")},
                    "detailedMetadataSnippets": [
                        {"snippetText": {"runs": [{"text": v.get("snippet", "")}]}}
                    ],
                }
            }
        )
    data = {"contents": {"twoColumnSearchResultsRenderer": {"primaryContents": contents}}}
    return f"<html><body><script>var ytInitialData = {json.dumps(data)};</script></body></html>"


class _FakePage:
    def __init__(self, html: str) -> None:
        self._html = html

    async def goto(self, url: str, **kwargs):
        return None

    async def content(self) -> str:
        return self._html

    async def close(self) -> None:
        return None


class _FakeContext:
    def __init__(self, html: str) -> None:
        self._html = html

    async def new_page(self) -> _FakePage:
        return _FakePage(self._html)


class _FakeBrowserSession:
    def __init__(self, html: str) -> None:
        self._html = html

    async def __aenter__(self) -> _FakeContext:
        return _FakeContext(self._html)

    async def __aexit__(self, *exc_info: Any) -> None:
        return None


async def test_search_falls_back_to_serp_without_api_key(monkeypatch):
    html = _build_serp_html(
        {
            "video_id": "vid001",
            "title": "Santos congressional speech",
            "channel": "C-SPAN",
            "views": "100K views",
            "published": "2 weeks ago",
            "snippet": "Floor speech delivered by Rep. Santos.",
        },
        {
            "video_id": "vid002",
            "title": "Santos commentary",
            "channel": "Pundit",
            "views": "5.2K views",
            "published": "1 week ago",
            "snippet": "Reaction to the speech.",
        },
    )

    from research_agent.tools import browser as browser_mod

    async def _no_throttle(url):
        return None

    monkeypatch.setattr(browser_mod, "browser_session", lambda **kw: _FakeBrowserSession(html))
    monkeypatch.setattr(
        browser_mod,
        "navigate",
        lambda page, url, timeout_ms=30000: _no_throttle(url),
    )

    results = await youtube.search("george santos", max_results=5)
    assert len(results) == 2
    assert results[0].url == "https://www.youtube.com/watch?v=vid001"
    assert results[0].title == "Santos congressional speech"
    assert results[0].extras["channel"] == "C-SPAN"
    assert results[0].extras["fetched_via"] == "youtube-serp"
    assert results[0].extras["view_count_text"] == "100K views"
    assert results[0].snippet.startswith("Floor speech")


async def test_search_serp_returns_empty_when_no_initial_data(monkeypatch):
    from research_agent.tools import browser as browser_mod

    monkeypatch.setattr(
        browser_mod,
        "browser_session",
        lambda **kw: _FakeBrowserSession("<html><body>nothing here</body></html>"),
    )

    async def _navigate(page, url, timeout_ms=30000):
        return None

    monkeypatch.setattr(browser_mod, "navigate", _navigate)
    assert await youtube.search("anything") == []


async def test_search_serp_returns_empty_on_browser_error(monkeypatch):
    from research_agent.tools import browser as browser_mod

    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("playwright not installed")

        async def __aexit__(self, *exc_info):
            return None

    monkeypatch.setattr(browser_mod, "browser_session", lambda **kw: _Boom())
    assert await youtube.search("anything") == []


# ---------------------------------------------------------------------------
# web_fetch dispatch
# ---------------------------------------------------------------------------


async def test_web_fetch_routes_youtube_to_youtube_fetch(monkeypatch):
    captured_urls: list[str] = []

    async def _fake_youtube_fetch(url):
        captured_urls.append(url)
        from datetime import UTC, datetime

        from research_agent.tools.models import Source

        return Source(
            url=url,
            title="t",
            cleaned_text="dispatched body",
            fetched_at=datetime.now(UTC),
            source_kind="web",
            metadata={"fetched_via": "yt-dlp-captions"},
        )

    monkeypatch.setattr(youtube, "fetch", _fake_youtube_fetch)

    async def _explode_httpx(*args, **kwargs):
        raise AssertionError("httpx should be skipped for youtube URLs")

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _explode_httpx)

    src = await web_fetch.fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert src is not None
    assert src.cleaned_text == "dispatched body"
    assert captured_urls == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]


async def test_web_fetch_routes_youtu_be_short_url(monkeypatch):
    captured: list[str] = []

    async def _fake_youtube_fetch(url):
        captured.append(url)
        return None

    monkeypatch.setattr(youtube, "fetch", _fake_youtube_fetch)

    async def _explode_httpx(*args, **kwargs):
        raise AssertionError("httpx should be skipped for youtu.be URLs")

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _explode_httpx)

    await web_fetch.fetch("https://youtu.be/dQw4w9WgXcQ")
    assert captured == ["https://youtu.be/dQw4w9WgXcQ"]


# ---------------------------------------------------------------------------
# Smoke registry wiring
# ---------------------------------------------------------------------------


def test_tool_registry_has_youtube():
    from research_agent.tools import TOOL_REGISTRY

    assert "youtube" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["youtube"])


def test_smoke_youtube_summary_matches_contract(monkeypatch):
    """The smoke verb summary lists total + channel/url/views per hit."""
    from research_agent.tools.models import SearchResult

    fake_results = [
        SearchResult(
            url="https://www.youtube.com/watch?v=vid001",
            title="Santos hearing live",
            snippet="Coverage of the floor speech.",
            source_kind="web",
            extras={
                "channel": "C-SPAN",
                "video_id": "vid001",
                "view_count_text": "100K views",
                "published_text": "2 weeks ago",
                "fetched_via": "youtube-serp",
            },
        )
    ]

    async def _fake_search(query, max_results=10):
        return fake_results

    monkeypatch.setattr(youtube, "search", _fake_search)

    from research_agent.tools import _smoke_youtube

    out = _smoke_youtube("george santos")
    assert "total: 1" in out
    assert "Santos hearing live" in out
    assert "https://www.youtube.com/watch?v=vid001" in out
    assert "C-SPAN" in out
    assert "views=100K views" in out


# ---------------------------------------------------------------------------
# Config wiring
# ---------------------------------------------------------------------------


def test_youtube_api_key_in_expected_env_keys():
    keys = {k.name for k in config_module.EXPECTED_ENV_KEYS}
    assert "YOUTUBE_API_KEY" in keys
