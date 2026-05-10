"""Tests for ``research_agent.tools.persee`` (issue #239)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from research_agent.tools import browser, persee
from research_agent.tools.models import SearchResult, Source

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "persee"


@pytest.fixture(autouse=True)
def _reset_state():
    browser.reset_for_tests()
    persee.reset_for_tests()
    yield
    browser.reset_for_tests()
    persee.reset_for_tests()


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

    monkeypatch.setattr(persee.browser, "browser_session", _browser_session)
    monkeypatch.setattr(persee.browser, "navigate", _navigate)
    return captured


async def test_search_happy_path_parses_fixture_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_browser(monkeypatch, _fixture("search-guerre-algerie.html"))

    results = await persee.search("guerre d'Algerie", max_results=20)

    assert captured["urls"] == [
        "https://www.persee.fr/search?ta=article&q=guerre+d%27Algerie"
    ]
    assert len(results) == 2

    first = results[0]
    assert first.source_kind == "persee_search"
    assert first.title == "La Guerre d'Algerie dans la presse francaise"
    assert first.url == "https://www.persee.fr/doc/mat_0769-3206_1992_num_26_1_404869"
    assert first.published_at == datetime(1992, 1, 1, tzinfo=UTC)
    assert "guerre d'Algerie" in first.snippet
    assert first.extras["doi"] == "10.3406/mat.1992.404869"
    assert first.extras["journal"] == "Materiaux pour l'histoire de notre temps"
    assert first.extras["volume"] == "26"
    assert first.extras["pub_year"] == "1992"
    assert first.extras["authors"] == ["Martine Lemaitre", "Jean Martin"]
    assert first.extras["lang"] == "fr"

    second = results[1]
    assert second.extras["doi"] == ""
    assert second.extras["journal"] == "Revue d'histoire moderne et contemporaine"
    assert second.extras["volume"] == "8-2"
    assert second.extras["pub_year"] == "1961"


async def test_fetch_happy_path_populates_source_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://www.persee.fr/doc/mat_0769-3206_1992_num_26_1_404869"
    _patch_browser(monkeypatch, _fixture("article.html"))

    source = await persee.fetch(url)

    assert source is not None
    assert source.source_kind == "persee_search"
    assert source.url == url
    assert source.title == "La Guerre d'Algerie dans la presse francaise"
    assert source.metadata["doi"] == "10.3406/mat.1992.404869"
    assert source.metadata["journal"] == "Materiaux pour l'histoire de notre temps"
    assert source.metadata["volume"] == "26"
    assert source.metadata["pub_year"] == "1992"
    assert source.metadata["authors"] == ["Martine Lemaitre", "Jean Martin"]
    assert source.metadata["lang"] == "fr"
    assert "## Article text" in source.cleaned_text
    assert "debats de presse" in source.cleaned_text


async def test_fetch_selector_drift_warning_writes_diagnostic_dump(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    url = "https://www.persee.fr/doc/mat_0769-3206_1992_num_26_1_404869"
    _patch_browser(monkeypatch, "<html lang='fr'><body><main></main></body></html>")
    monkeypatch.setattr(persee, "_DIAGNOSTICS_DIR", tmp_path)

    with caplog.at_level(logging.WARNING):
        source = await persee.fetch(url)

    assert source is None
    assert "persee fetch selector drift" in caplog.text
    assert list(tmp_path.glob("fetch-selector-drift-*.html"))
    assert list(tmp_path.glob("fetch-selector-drift-*.png"))


async def test_search_empty_fixture_returns_empty_without_drift_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _patch_browser(monkeypatch, _fixture("search-empty.html"))

    with caplog.at_level(logging.WARNING):
        results = await persee.search("definitely absent", max_results=20)

    assert results == []
    assert "selector drift" not in caplog.text


async def test_selector_drift_warning_writes_diagnostic_dump(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _patch_browser(monkeypatch, _fixture("search-drift.html"))
    monkeypatch.setattr(persee, "_DIAGNOSTICS_DIR", tmp_path)

    with caplog.at_level(logging.WARNING):
        results = await persee.search("guerre d'Algerie", max_results=20)

    assert results == []
    assert "persee search selector drift" in caplog.text
    assert list(tmp_path.glob("search-selector-drift-*.html"))
    assert list(tmp_path.glob("search-selector-drift-*.png"))


async def test_rate_limit_is_half_rps(monkeypatch: pytest.MonkeyPatch) -> None:
    browser.reset_for_tests()
    persee.reset_for_tests()
    clock = [100.0]
    sleep_calls: list[float] = []

    def _monotonic() -> float:
        return clock[0]

    async def _sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(browser.time, "monotonic", _monotonic)
    monkeypatch.setattr(browser.asyncio, "sleep", _sleep)

    await browser.throttle("https://www.persee.fr/search")
    clock[0] += 0.25
    await browser.throttle("https://www.persee.fr/doc/mat_0769")

    assert sleep_calls == pytest.approx([1.75])


async def test_fetch_rejects_foreign_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(*_args, **_kwargs):
        raise AssertionError("foreign hosts must not launch Playwright")

    monkeypatch.setattr(persee.browser, "browser_session", _boom)

    assert await persee.fetch("https://example.com/doc/mat") is None
    assert await persee.fetch("https://www.persee.fr.attacker.example/doc/mat") is None


def test_source_kind_accepts_persee_search() -> None:
    result = SearchResult(
        url="https://www.persee.fr/doc/mat_0769-3206_1992_num_26_1_404869",
        title="t",
        snippet="s",
        source_kind="persee_search",
    )
    assert result.source_kind == "persee_search"
    source = Source(
        url="https://www.persee.fr/doc/mat_0769-3206_1992_num_26_1_404869",
        title="t",
        cleaned_text="metadata",
        fetched_at=datetime.now(UTC),
        source_kind="persee_search",
    )
    assert source.source_kind == "persee_search"


def test_smoke_registry_includes_persee_search() -> None:
    from research_agent.tools import TOOL_REGISTRY

    assert "persee_search" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["persee_search"])


def test_smoke_wrapper_requires_non_empty_persee_title_and_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research_agent.tools import TOOL_REGISTRY

    async def fake_search(query: str, *, max_results: int = 20):
        assert query == "guerre d'Algerie"
        assert max_results == 5
        return [
            SearchResult(
                url="https://www.persee.fr/doc/mat_0769-3206_1992_num_26_1_404869",
                title="La Guerre d'Algerie dans la presse francaise",
                snippet="Materiaux pour l'histoire de notre temps Annee 1992",
                source_kind="persee_search",
                extras={
                    "doi": "10.3406/mat.1992.404869",
                    "journal": "Materiaux pour l'histoire de notre temps",
                    "pub_year": "1992",
                    "authors": ["Martine Lemaitre"],
                },
            )
        ]

    monkeypatch.setattr(persee, "search", fake_search)

    out = TOOL_REGISTRY["persee_search"]("guerre d'Algerie")

    assert "persee_search: returned 1 hits for query: guerre d'Algerie" in out
    assert "La Guerre d'Algerie" in out
    assert "https://www.persee.fr/doc/mat_0769-3206_1992_num_26_1_404869" in out
    assert "journal: Materiaux pour l'histoire de notre temps" in out
    assert "doi: 10.3406/mat.1992.404869" in out
    assert "snippet:" not in out


def test_smoke_wrapper_omits_missing_optional_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research_agent.tools import TOOL_REGISTRY

    async def fake_search(query: str, *, max_results: int = 20):
        assert query == "guerre d'Algerie"
        assert max_results == 5
        return [
            SearchResult(
                url="https://www.persee.fr/doc/xxs_0294-1759_2001_num_70_1_1356",
                title="La guerre d'Algerie",
                snippet="La guerre d'Algerie",
                source_kind="persee_search",
                extras={
                    "doi": "",
                    "journal": "Vingtieme Siecle. Revue d'histoire",
                    "pub_year": "2001",
                    "authors": [],
                },
            )
        ]

    monkeypatch.setattr(persee, "search", fake_search)

    out = TOOL_REGISTRY["persee_search"]("guerre d'Algerie")

    assert "query: guerre d'Algerie" in out
    assert "none listed" not in out
    assert "..." not in out
    assert "doi:" not in out
    assert "authors:" not in out
    assert "journal: Vingtieme Siecle. Revue d'histoire" in out


def test_registered_kind_links_persee_skill() -> None:
    from research_agent.tools._registry import get_kind

    entry = get_kind("persee_search")
    assert entry is not None
    assert entry.skill_name == "persee"
    assert entry.fetch_fn is persee.fetch
    assert entry.host_patterns == ("www.persee.fr", "persee.fr")


def test_doctor_registry_skill_coherence_passes_for_persee() -> None:
    from research_agent import doctor

    rows = doctor.check_registry_skill_coherence()
    persee_rows = [row for row in rows if row.name == "registry_skill:persee_search"]

    assert len(persee_rows) == 1
    assert persee_rows[0].status == "ok"


def test_persee_skill_covers_required_operator_topics() -> None:
    from research_agent.skills import load_skill

    body = load_skill("connectors", "persee")

    for needle in (
        "Playwright scrape",
        "public API is partial",
        "humanities and social sciences",
        "colonial-era studies",
        "Annales-school history",
        'metadata["doi"]',
        "web_fetch",
        "full PDF",
        "multilingual-source-handling",
        "FR-first",
        "English-only",
        "No auth",
    ):
        assert needle in body
