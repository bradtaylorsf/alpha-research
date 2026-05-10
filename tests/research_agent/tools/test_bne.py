"""Tests for ``research_agent.tools.bne`` (issue #240)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from research_agent.tools import bne, browser
from research_agent.tools.models import SearchResult, Source

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "bne"


@pytest.fixture(autouse=True)
def _reset_state():
    browser.reset_for_tests()
    bne.reset_for_tests()
    yield
    browser.reset_for_tests()
    bne.reset_for_tests()


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


def _patch_browser(monkeypatch: pytest.MonkeyPatch, html: str) -> dict[str, object]:
    page = _FakePage(html)
    captured: dict[str, object] = {"urls": [], "page": page}

    @asynccontextmanager
    async def _browser_session(*_args, **_kwargs):
        yield _FakeContext(page)

    async def _navigate(_page: _FakePage, url: str, **_kwargs) -> None:
        assert _page is page
        captured["urls"].append(url)  # type: ignore[union-attr]

    monkeypatch.setattr(bne.browser, "browser_session", _browser_session)
    monkeypatch.setattr(bne.browser, "navigate", _navigate)
    return captured


async def test_search_happy_path_parses_fixture_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_browser(monkeypatch, _fixture("search-guerra-civil.html"))

    results = await bne.search("guerra civil 1936", max_results=20)

    assert captured["urls"] == [
        "https://hemerotecadigital.bne.es/hd/es/results?text=guerra+civil+1936"
    ]
    assert len(results) == 2

    first = results[0]
    assert first.source_kind == "bne_search"
    assert first.title == "El Liberal (Madrid. 1879). 19/7/1936 [Ejemplar]"
    assert (
        first.url
        == "https://hemerotecadigital.bne.es/hd/es/results?id=7b8f8c35-0d6e-4f60-90c1-111111111111&oid=0030246855"
    )
    assert first.published_at == datetime(1936, 7, 19, tzinfo=UTC)
    assert "guerra civil" in first.snippet
    assert first.extras["publication"] == "El Liberal"
    assert first.extras["pub_date"] == "1936-07-19"
    assert first.extras["place"] == "Madrid"
    assert first.extras["lang"] == "spa"
    assert (
        first.extras["fulltext_url"]
        == "https://hemerotecadigital.bne.es/hd/es/download?id=7b8f8c35-0d6e-4f60-90c1-111111111111&oid=0030246855"
    )

    second = results[1]
    assert second.extras["publication"] == "Ahora"
    assert second.extras["pub_date"] == "1936-07-21"


async def test_search_url_supports_date_and_place_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_browser(monkeypatch, _fixture("search-empty.html"))

    await bne.search(
        "guerra civil",
        max_results=20,
        fechaDesde="1936-07-01",
        fechaHasta="1936-07-31",
        localizacion="Madrid",
    )

    assert captured["urls"] == [
        "https://hemerotecadigital.bne.es/hd/es/results?text=guerra+civil&fechaDesde=1936-07-01&fechaHasta=1936-07-31&localizacion=Madrid"
    ]


async def test_fetch_happy_path_populates_source_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = (
        "https://hemerotecadigital.bne.es/hd/es/results"
        "?id=7b8f8c35-0d6e-4f60-90c1-111111111111&oid=0030246855"
    )
    _patch_browser(monkeypatch, _fixture("issue.html"))

    source = await bne.fetch(url)

    assert source is not None
    assert source.source_kind == "bne_search"
    assert source.url == url
    assert source.title == "El Liberal (Madrid. 1879). 19/7/1936 [Ejemplar]"
    assert source.metadata["publication"] == "El Liberal"
    assert source.metadata["pub_date"] == "1936-07-19"
    assert source.metadata["place"] == "Madrid"
    assert source.metadata["lang"] == "spa"
    assert (
        source.metadata["fulltext_url"]
        == "https://hemerotecadigital.bne.es/hd/es/download?id=7b8f8c35-0d6e-4f60-90c1-111111111111&oid=0030246855"
    )
    assert "## Visible page text" in source.cleaned_text
    assert "guerra civil" in source.cleaned_text


async def test_fetch_selector_drift_warning_writes_diagnostic_dump(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    url = (
        "https://hemerotecadigital.bne.es/hd/es/results"
        "?id=7b8f8c35-0d6e-4f60-90c1-111111111111&oid=0030246855"
    )
    _patch_browser(monkeypatch, "<html lang='es'><body><main></main></body></html>")
    monkeypatch.setattr(bne, "_DIAGNOSTICS_DIR", tmp_path)

    with caplog.at_level(logging.WARNING):
        source = await bne.fetch(url)

    assert source is None
    assert "bne fetch selector drift" in caplog.text
    assert list(tmp_path.glob("fetch-selector-drift-*.html"))
    assert list(tmp_path.glob("fetch-selector-drift-*.png"))


async def test_search_empty_fixture_returns_empty_without_drift_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _patch_browser(monkeypatch, _fixture("search-empty.html"))

    with caplog.at_level(logging.WARNING):
        results = await bne.search("definitely absent", max_results=20)

    assert results == []
    assert "selector drift" not in caplog.text


async def test_selector_drift_warning_writes_diagnostic_dump(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _patch_browser(monkeypatch, _fixture("search-drift.html"))
    monkeypatch.setattr(bne, "_DIAGNOSTICS_DIR", tmp_path)

    with caplog.at_level(logging.WARNING):
        results = await bne.search("guerra civil 1936", max_results=20)

    assert results == []
    assert "bne search selector drift" in caplog.text
    assert list(tmp_path.glob("search-selector-drift-*.html"))
    assert list(tmp_path.glob("search-selector-drift-*.png"))


async def test_rate_limit_is_half_rps(monkeypatch: pytest.MonkeyPatch) -> None:
    browser.reset_for_tests()
    bne.reset_for_tests()
    clock = [100.0]
    sleep_calls: list[float] = []

    def _monotonic() -> float:
        return clock[0]

    async def _sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(browser.time, "monotonic", _monotonic)
    monkeypatch.setattr(browser.asyncio, "sleep", _sleep)

    await browser.throttle("https://hemerotecadigital.bne.es/hd/es/results")
    clock[0] += 0.25
    await browser.throttle("https://hemerotecadigital.bne.es/hd/es/results?id=abc")

    assert sleep_calls == pytest.approx([1.75])


async def test_fetch_rejects_foreign_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(*_args, **_kwargs):
        raise AssertionError("foreign hosts must not launch Playwright")

    monkeypatch.setattr(bne.browser, "browser_session", _boom)

    assert await bne.fetch("https://example.com/hd/es/results?id=abc") is None
    attacker_url = "https://hemerotecadigital.bne.es.attacker.example/hd/es/results"
    assert await bne.fetch(attacker_url) is None


def test_source_kind_accepts_bne_search() -> None:
    result = SearchResult(
        url="https://hemerotecadigital.bne.es/hd/es/results?id=abc",
        title="t",
        snippet="s",
        source_kind="bne_search",
    )
    assert result.source_kind == "bne_search"
    source = Source(
        url="https://hemerotecadigital.bne.es/hd/es/results?id=abc",
        title="t",
        cleaned_text="metadata",
        fetched_at=datetime.now(UTC),
        source_kind="bne_search",
    )
    assert source.source_kind == "bne_search"


def test_smoke_registry_includes_bne_search() -> None:
    from research_agent.tools import TOOL_REGISTRY

    assert "bne_search" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["bne_search"])


def test_smoke_wrapper_requires_non_empty_bne_title_and_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research_agent.tools import TOOL_REGISTRY

    async def fake_search(query: str, *, max_results: int = 20):
        assert query == "guerra civil 1936"
        assert max_results == 5
        return [
            SearchResult(
                url="https://hemerotecadigital.bne.es/hd/es/results?id=abc&oid=1",
                title="El Liberal (Madrid. 1879). 19/7/1936 [Ejemplar]",
                snippet="guerra civil",
                source_kind="bne_search",
                extras={
                    "publication": "El Liberal",
                    "pub_date": "1936-07-19",
                    "place": "Madrid",
                    "lang": "spa",
                    "fulltext_url": "https://hemerotecadigital.bne.es/hd/es/download?id=abc&oid=1",
                },
            )
        ]

    async def fake_shutdown() -> None:
        return None

    monkeypatch.setattr(bne, "search", fake_search)
    monkeypatch.setattr(bne.browser, "shutdown", fake_shutdown)

    out = TOOL_REGISTRY["bne_search"]("guerra civil 1936")

    assert "bne_search: returned 1 hits for query: guerra civil 1936" in out
    assert "El Liberal" in out
    assert "https://hemerotecadigital.bne.es/hd/es/results?id=abc&oid=1" in out
    assert "publication: El Liberal" in out
    assert "date: 1936-07-19" in out
    assert "fulltext_url: https://hemerotecadigital.bne.es/hd/es/download?id=abc&oid=1" in out


def test_registered_kind_links_bne_skill() -> None:
    from research_agent.tools._registry import get_kind

    entry = get_kind("bne_search")
    assert entry is not None
    assert entry.skill_name == "bne"
    assert entry.fetch_fn is bne.fetch
    assert "hemerotecadigital.bne.es" in entry.host_patterns
    assert "fechaDesde" in entry.optional_payload_knobs
    assert "localizacion" in entry.optional_payload_knobs


def test_doctor_registry_skill_coherence_passes_for_bne() -> None:
    from research_agent import doctor

    rows = doctor.check_registry_skill_coherence()
    bne_rows = [row for row in rows if row.name == "registry_skill:bne_search"]

    assert len(bne_rows) == 1
    assert bne_rows[0].status == "ok"


def test_bne_skill_covers_required_operator_topics() -> None:
    from research_agent.skills import load_skill

    body = load_skill("connectors", "bne")

    for needle in (
        "Playwright scrape",
        "2024-2025 migration",
        "Latin-American independence movements",
        "Spanish Civil War",
        "Franco era",
        "post-Franco transition",
        "colonial-era press",
        "text=<query>",
        "fechaDesde",
        "fechaHasta",
        "localizacion",
        'metadata["fulltext_url"]',
        "digitized periodical",
        "multilingual-source-handling",
        "No auth",
    ):
        assert needle in body
