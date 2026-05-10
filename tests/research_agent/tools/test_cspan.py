"""Tests for ``research_agent.tools.cspan`` (issue #242)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from research_agent.tools import browser, cspan
from research_agent.tools.models import SearchResult, Source

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "cspan"


@pytest.fixture(autouse=True)
def _reset_state():
    browser.reset_for_tests()
    cspan.reset_for_tests()
    yield
    browser.reset_for_tests()
    cspan.reset_for_tests()


class _FakePage:
    def __init__(self, html: str) -> None:
        self._html = html
        self.closed = False

    async def content(self) -> str:
        return self._html

    async def close(self) -> None:
        self.closed = True

    async def screenshot(self, *, path: str) -> None:
        Path(path).write_text("fake screenshot", encoding="utf-8")


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.page = page

    async def new_page(self) -> _FakePage:
        return self.page


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _source_fixture() -> str:
    transcript = _fixture("transcript.json")
    return _fixture("source-program.html").replace("__TRANSCRIPT_JSON__", transcript)


def _patch_browser(monkeypatch: pytest.MonkeyPatch, html: str) -> dict[str, object]:
    page = _FakePage(html)
    captured: dict[str, object] = {"urls": [], "page": page}

    @asynccontextmanager
    async def _browser_session(*_args, **_kwargs):
        yield _FakeContext(page)

    async def _navigate(_page: _FakePage, url: str, **_kwargs) -> None:
        assert _page is page
        captured["urls"].append(url)  # type: ignore[union-attr]

    monkeypatch.setattr(cspan.browser, "browser_session", _browser_session)
    monkeypatch.setattr(cspan.browser, "navigate", _navigate)
    return captured


async def test_search_happy_path_parses_fixture_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_browser(monkeypatch, _fixture("search-project-2025.html"))

    results = await cspan.search("Project 2025", max_results=20)

    assert captured["urls"] == [
        "https://www.c-span.org/search/?searchtype=Videos&query=Project+2025"
    ]
    assert len(results) == 2

    first = results[0]
    assert first.source_kind == "cspan_search"
    assert first.title == "Project 2025 Presidential Transition"
    assert (
        first.url
        == "https://www.c-span.org/program/public-affairs-event/project-2025-presidential-transition/654321"
    )
    assert first.published_at == datetime(2024, 6, 11, tzinfo=UTC)
    assert "Witnesses testified" in first.snippet
    assert first.extras["program_id"] == "654321"
    assert first.extras["air_date"] == "2024-06-11"
    assert first.extras["duration_seconds"] == 5025
    assert first.extras["video_url"] == first.url

    second = results[1]
    assert second.extras["program_id"] == "654322"
    assert second.extras["air_date"] == "2024-06-12"
    assert second.extras["duration_seconds"] == 743


async def test_search_url_supports_house_senate_type_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_browser(monkeypatch, _fixture("search-empty.html"))

    await cspan.search("Project 2025", max_results=20, type="Senate")

    assert captured["urls"] == [
        "https://www.c-span.org/search/?searchtype=Videos&query=Project+2025&type=Senate"
    ]


async def test_fetch_happy_path_populates_source_metadata_and_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = (
        "https://www.c-span.org/program/public-affairs-event/"
        "project-2025-presidential-transition/654321"
    )
    _patch_browser(monkeypatch, _source_fixture())

    source = await cspan.fetch(url)

    assert source is not None
    assert source.source_kind == "cspan_search"
    assert source.url == url
    assert source.title == "Project 2025 Presidential Transition"
    assert source.metadata["program_id"] == "654321"
    assert source.metadata["air_date"] == "2024-06-11"
    assert source.metadata["duration_seconds"] == 5025
    assert source.metadata["video_url"] == "https://static.c-span.org/video/654321.mp4"
    assert "Jamie Raskin" in source.metadata["speakers"]
    assert "Heidi Przybyla" in source.metadata["speakers"]
    assert "## Transcript" in source.cleaned_text
    assert "The Project 2025 plan calls for a sweeping reorganization" in source.cleaned_text
    assert "Schedule F and the presidential transition infrastructure" in source.cleaned_text
    assert "metadata" not in source.cleaned_text.lower()


async def test_transcript_is_in_cleaned_text_not_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = (
        "https://www.c-span.org/program/public-affairs-event/"
        "project-2025-presidential-transition/654321"
    )
    _patch_browser(monkeypatch, _source_fixture())

    source = await cspan.fetch(url)

    assert source is not None
    assert "transcript" not in source.metadata
    assert "Project 2025 plan calls" in source.cleaned_text


async def test_search_empty_fixture_returns_empty_without_drift_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _patch_browser(monkeypatch, _fixture("search-empty.html"))

    with caplog.at_level(logging.WARNING):
        results = await cspan.search("definitely absent", max_results=20)

    assert results == []
    assert "selector drift" not in caplog.text


async def test_selector_drift_warning_writes_diagnostic_dump(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _patch_browser(monkeypatch, _fixture("search-drift.html"))
    monkeypatch.setattr(cspan, "_DIAGNOSTICS_DIR", tmp_path)

    with caplog.at_level(logging.WARNING):
        results = await cspan.search("Project 2025", max_results=20)

    assert results == []
    assert "cspan search selector drift" in caplog.text
    assert list(tmp_path.glob("search-selector-drift-*.html"))
    assert list(tmp_path.glob("search-selector-drift-*.png"))


async def test_fetch_selector_drift_warning_writes_diagnostic_dump(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    url = (
        "https://www.c-span.org/program/public-affairs-event/"
        "project-2025-presidential-transition/654321"
    )
    _patch_browser(monkeypatch, "<html><body><main></main></body></html>")
    monkeypatch.setattr(cspan, "_DIAGNOSTICS_DIR", tmp_path)

    with caplog.at_level(logging.WARNING):
        source = await cspan.fetch(url)

    assert source is None
    assert "cspan fetch selector drift" in caplog.text
    assert list(tmp_path.glob("fetch-selector-drift-*.html"))
    assert list(tmp_path.glob("fetch-selector-drift-*.png"))


async def test_rate_limit_is_half_rps(monkeypatch: pytest.MonkeyPatch) -> None:
    browser.reset_for_tests()
    cspan.reset_for_tests()
    clock = [100.0]
    sleep_calls: list[float] = []

    def _monotonic() -> float:
        return clock[0]

    async def _sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(browser.time, "monotonic", _monotonic)
    monkeypatch.setattr(browser.asyncio, "sleep", _sleep)

    await browser.throttle("https://www.c-span.org/search/")
    clock[0] += 0.25
    await browser.throttle("https://www.c-span.org/program/public-affairs-event/x/654321")

    assert sleep_calls == pytest.approx([1.75])


async def test_fetch_rejects_foreign_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(*_args, **_kwargs):
        raise AssertionError("foreign hosts must not launch Playwright")

    monkeypatch.setattr(cspan.browser, "browser_session", _boom)

    assert await cspan.fetch("https://example.com/program/public-affairs-event/x/654321") is None
    assert await cspan.fetch("https://www.c-span.org.attacker.example/program/x/654321") is None


def test_source_kind_accepts_cspan_search() -> None:
    result = SearchResult(
        url="https://www.c-span.org/program/public-affairs-event/x/654321",
        title="t",
        snippet="s",
        source_kind="cspan_search",
    )
    assert result.source_kind == "cspan_search"
    source = Source(
        url="https://www.c-span.org/program/public-affairs-event/x/654321",
        title="t",
        cleaned_text="metadata",
        fetched_at=datetime.now(UTC),
        source_kind="cspan_search",
    )
    assert source.source_kind == "cspan_search"


def test_smoke_registry_includes_cspan_search() -> None:
    from research_agent.tools import TOOL_REGISTRY

    assert "cspan_search" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["cspan_search"])


def test_smoke_wrapper_requires_non_empty_title_url_and_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research_agent.tools import TOOL_REGISTRY

    async def fake_search(query: str, *, max_results: int = 20):
        assert query == "Project 2025"
        assert max_results == 5
        return [
            SearchResult(
                url="https://www.c-span.org/program/public-affairs-event/project-2025/654321",
                title="Project 2025 Presidential Transition",
                snippet="Project 2025 hearing",
                source_kind="cspan_search",
                extras={
                    "program_id": "654321",
                    "air_date": "2024-06-11",
                    "duration_seconds": 5025,
                    "video_url": "https://www.c-span.org/program/public-affairs-event/project-2025/654321",
                },
            )
        ]

    async def fake_fetch(url: str):
        return Source(
            url=url,
            title="Project 2025 Presidential Transition",
            cleaned_text=(
                "# Project 2025\n\n## Transcript\n\n"
                "Jamie Raskin: Project 2025 plan calls..."
            ),
            fetched_at=datetime.now(UTC),
            source_kind="cspan_search",
            metadata={
                "program_id": "654321",
                "air_date": "2024-06-11",
                "duration_seconds": 5025,
                "video_url": url,
                "speakers": ["Jamie Raskin"],
            },
        )

    async def fake_shutdown() -> None:
        return None

    monkeypatch.setattr(cspan, "search", fake_search)
    monkeypatch.setattr(cspan, "fetch", fake_fetch)
    monkeypatch.setattr(cspan.browser, "shutdown", fake_shutdown)

    out = TOOL_REGISTRY["cspan_search"]("Project 2025")

    assert "cspan_search: returned 1 hits for query: Project 2025" in out
    assert "Project 2025 Presidential Transition" in out
    assert "program_id: 654321" in out
    assert "fetched_transcript:" in out
    assert "Jamie Raskin" in out


def test_registered_kind_links_cspan_skill() -> None:
    from research_agent.tools._registry import get_kind

    entry = get_kind("cspan_search")
    assert entry is not None
    assert entry.skill_name == "cspan"
    assert entry.fetch_fn is cspan.fetch
    assert "www.c-span.org" in entry.host_patterns
    assert "type=House\\|Senate" in entry.optional_payload_knobs


def test_doctor_registry_skill_coherence_passes_for_cspan() -> None:
    from research_agent import doctor

    rows = doctor.check_registry_skill_coherence()
    cspan_rows = [row for row in rows if row.name == "registry_skill:cspan_search"]

    assert len(cspan_rows) == 1
    assert cspan_rows[0].status == "ok"


def test_cspan_skill_covers_required_operator_topics() -> None:
    from research_agent.skills import load_skill

    body = load_skill("connectors", "cspan")

    for needle in (
        "Playwright scrape",
        "Source.cleaned_text",
        'Source.metadata["transcript"]',
        "post-1979 US political broadcast",
        "congressional floor speeches",
        "presidential events",
        "&type=Senate",
        "&type=House",
        'metadata["speakers"]',
        "one witness's testimony",
        "Do not expect transcripts for all events",
        "No auth",
    ):
        assert needle in body
