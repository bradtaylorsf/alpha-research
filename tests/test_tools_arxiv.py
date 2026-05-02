"""Tests for `research_agent.tools.arxiv_tool` (issue #19)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import arxiv
import pytest

from research_agent.tools import arxiv_tool

FIXTURE_PDF = Path(__file__).parent / "fixtures" / "arxiv_paper.pdf"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_result(
    *,
    arxiv_id: str = "2401.12345",
    title: str = "A Sample arXiv Paper",
    summary: str = "We study transformer interpretability via probing.",
    published: datetime | None = None,
    authors: list[str] | None = None,
    categories: list[str] | None = None,
) -> arxiv.Result:
    """Construct a minimal `arxiv.Result` mirroring `_from_feed_entry` shape."""
    entry_id = f"https://arxiv.org/abs/{arxiv_id}"
    pdf_link = arxiv.Result.Link(
        href=f"https://arxiv.org/pdf/{arxiv_id}",
        title="pdf",
        rel="related",
        content_type="application/pdf",
    )
    abs_link = arxiv.Result.Link(
        href=entry_id, title=None, rel="alternate", content_type="text/html"
    )
    return arxiv.Result(
        entry_id=entry_id,
        updated=published or datetime(2026, 1, 15, tzinfo=UTC),
        published=published or datetime(2026, 1, 15, tzinfo=UTC),
        title=title,
        authors=[arxiv.Result.Author(name) for name in (authors or ["Ada Lovelace"])],
        summary=summary,
        primary_category=(categories or ["cs.LG"])[0],
        categories=categories or ["cs.LG"],
        links=[abs_link, pdf_link],
    )


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    monkeypatch.delenv("RESEARCH_USER_AGENT", raising=False)
    yield


@pytest.fixture(autouse=True)
def _reset_arxiv_state(monkeypatch):
    arxiv_tool.reset_for_tests()
    monkeypatch.setattr(arxiv_tool.asyncio, "sleep", AsyncMock())
    yield
    arxiv_tool.reset_for_tests()


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "arxiv-cache"
    monkeypatch.setattr(arxiv_tool, "_CACHE_DIR", target)
    return target


def _patch_client_results(monkeypatch, results: list[arxiv.Result]):
    """Replace `arxiv.Client.results` with a stub that yields `results`."""
    captured: dict[str, object] = {"call_count": 0, "searches": []}

    def _fake_results(self, search, offset=0):  # noqa: ARG001
        captured["call_count"] = int(captured["call_count"]) + 1
        captured["searches"].append(search)
        return iter(results)

    monkeypatch.setattr(arxiv_tool.arxiv.Client, "results", _fake_results)
    return captured


def _patch_httpx(monkeypatch, *, body: bytes, status_code: int = 200):
    """Replace `httpx.AsyncClient` with a fake whose ``get`` returns ``body``."""
    captured: dict[str, object] = {"call_count": 0, "urls": []}

    class _FakeResp:
        def __init__(self, body: bytes, status_code: int) -> None:
            self.content = body
            self.status_code = status_code

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):  # noqa: ARG001
        captured["init_kwargs"] = kwargs

        class _Client:
            async def get(self, url, *_args, **_kwargs):
                captured["call_count"] = int(captured["call_count"]) + 1
                captured["urls"].append(url)
                return _FakeResp(body, status_code)

        yield _Client()

    monkeypatch.setattr(arxiv_tool.httpx, "AsyncClient", _client_factory)
    return captured


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_returns_search_results_with_arxiv_kind(monkeypatch):
    fake = _make_result(
        title="Transformer Interpretability via Probing",
        summary="We probe attention heads in GPT-style models.",
    )
    _patch_client_results(monkeypatch, [fake, _make_result(arxiv_id="2402.99999")])

    results = await arxiv_tool.search("transformer interpretability", max_results=5)

    assert len(results) == 2
    assert all(r.source_kind == "arxiv" for r in results)
    assert results[0].title == "Transformer Interpretability via Probing"
    assert "attention heads" in results[0].snippet
    assert results[0].url == "https://arxiv.org/abs/2401.12345"
    assert results[0].extras["arxiv_id"] == "2401.12345"
    assert results[0].extras["authors"] == ["Ada Lovelace"]
    assert results[0].extras["pdf_url"] == "https://arxiv.org/pdf/2401.12345"
    assert results[0].extras["categories"] == ["cs.LG"]


async def test_search_honors_sort_by_submitted_date(monkeypatch):
    captured = _patch_client_results(monkeypatch, [_make_result()])
    await arxiv_tool.search("foo", max_results=3, sort_by="submittedDate")

    assert captured["call_count"] == 1
    search_obj = captured["searches"][0]
    assert search_obj.sort_by == arxiv.SortCriterion.SubmittedDate
    assert search_obj.max_results == 3
    assert search_obj.query == "foo"


async def test_search_rejects_invalid_sort_by(monkeypatch):
    _patch_client_results(monkeypatch, [])
    with pytest.raises(ValueError, match="sort_by"):
        await arxiv_tool.search("foo", sort_by="bogus")


async def test_search_returns_empty_list_on_arxiv_error(monkeypatch):
    def _raise(self, search, offset=0):  # noqa: ARG001
        raise arxiv.HTTPError("https://arxiv.org/api", 0, 503)

    monkeypatch.setattr(arxiv_tool.arxiv.Client, "results", _raise)

    assert await arxiv_tool.search("foo") == []


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


async def test_fetch_downloads_and_extracts_pdf(monkeypatch, cache_dir: Path):
    pdf_bytes = FIXTURE_PDF.read_bytes()
    _patch_httpx(monkeypatch, body=pdf_bytes)
    # Stub title lookup to avoid a second arxiv.Client call path.
    _patch_client_results(monkeypatch, [_make_result(title="My Paper")])

    source = await arxiv_tool.fetch("2401.12345")

    assert source is not None
    assert source.source_kind == "arxiv"
    assert source.url == "https://arxiv.org/abs/2401.12345"
    assert source.cleaned_text  # non-empty
    assert "Hello arxiv research paper text" in source.cleaned_text
    assert source.title == "My Paper"
    assert source.metadata["arxiv_id"] == "2401.12345"
    assert source.metadata["pdf_url"] == "https://arxiv.org/pdf/2401.12345.pdf"
    assert (cache_dir / "2401.12345.pdf").exists()


async def test_fetch_is_idempotent_and_uses_cache(monkeypatch, cache_dir: Path):
    pdf_bytes = FIXTURE_PDF.read_bytes()
    captured = _patch_httpx(monkeypatch, body=pdf_bytes)
    _patch_client_results(monkeypatch, [_make_result()])

    s1 = await arxiv_tool.fetch("2401.12345")
    s2 = await arxiv_tool.fetch("2401.12345")

    assert s1 is not None and s2 is not None
    # HTTP download fired only once; the second call hits the cache.
    assert captured["call_count"] == 1


async def test_fetch_accepts_abs_url(monkeypatch, cache_dir: Path):
    _patch_httpx(monkeypatch, body=FIXTURE_PDF.read_bytes())
    _patch_client_results(monkeypatch, [_make_result()])

    source = await arxiv_tool.fetch("https://arxiv.org/abs/2401.12345v2")

    assert source is not None
    assert source.metadata["arxiv_id"] == "2401.12345"
    assert source.url == "https://arxiv.org/abs/2401.12345"


async def test_fetch_accepts_pdf_url(monkeypatch, cache_dir: Path):
    _patch_httpx(monkeypatch, body=FIXTURE_PDF.read_bytes())
    _patch_client_results(monkeypatch, [_make_result()])

    source = await arxiv_tool.fetch("https://arxiv.org/pdf/2401.12345.pdf")

    assert source is not None
    assert source.metadata["arxiv_id"] == "2401.12345"


async def test_fetch_returns_none_on_http_error(monkeypatch, cache_dir: Path):
    _patch_httpx(monkeypatch, body=b"", status_code=500)
    _patch_client_results(monkeypatch, [])

    assert await arxiv_tool.fetch("2401.12345") is None
    assert not (cache_dir / "2401.12345.pdf").exists()


async def test_fetch_returns_none_on_transport_error(monkeypatch, cache_dir: Path):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):  # noqa: ARG001
        class _Client:
            async def get(self, url, *_a, **_k):
                raise arxiv_tool.httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(arxiv_tool.httpx, "AsyncClient", _client_factory)

    assert await arxiv_tool.fetch("2401.12345") is None


async def test_fetch_returns_none_on_unparseable_id(monkeypatch, cache_dir: Path):
    assert await arxiv_tool.fetch("not-an-arxiv-id") is None


async def test_fetch_falls_back_to_id_as_title(monkeypatch, cache_dir: Path):
    """When the title lookup yields nothing, fall back to the bare id."""
    _patch_httpx(monkeypatch, body=FIXTURE_PDF.read_bytes())
    _patch_client_results(monkeypatch, [])  # title lookup returns no results

    source = await arxiv_tool.fetch("2401.12345")

    assert source is not None
    assert source.title == "2401.12345"


# ---------------------------------------------------------------------------
# Rate-limit gate
# ---------------------------------------------------------------------------


async def test_rate_limits_concurrent_calls(monkeypatch):
    """Two concurrent ``search`` calls must be serialised by the 3 s gate."""
    clock = [100.0]

    def fake_monotonic() -> float:
        return clock[0]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(arxiv_tool.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(arxiv_tool.asyncio, "sleep", fake_sleep)

    _patch_client_results(monkeypatch, [_make_result()])

    await asyncio.gather(
        arxiv_tool.search("foo"),
        arxiv_tool.search("bar"),
    )

    assert any(s >= arxiv_tool._RATE_LIMIT_INTERVAL for s in sleep_calls), (
        f"expected a sleep ≥ {arxiv_tool._RATE_LIMIT_INTERVAL}s, got {sleep_calls!r}"
    )


# ---------------------------------------------------------------------------
# Smoke registration
# ---------------------------------------------------------------------------


def test_smoke_registry_includes_arxiv():
    from research_agent.tools import TOOL_REGISTRY

    assert "arxiv" in TOOL_REGISTRY
