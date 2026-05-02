"""Tests for `research_agent.tools.news` (issue #20)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import struct_time
from types import SimpleNamespace
from typing import Any

import pytest

from research_agent.tools import news

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _struct(dt: datetime) -> struct_time:
    return dt.utctimetuple()


def _make_entry(
    *,
    title: str,
    link: str,
    summary: str = "",
    published: datetime | None = None,
) -> SimpleNamespace:
    """Mimic the ``feedparser`` entry dict (supports ``.get()`` and ``.``)."""
    data: dict[str, Any] = {
        "title": title,
        "link": link,
        "summary": summary,
    }
    if published is not None:
        data["published_parsed"] = _struct(published)
    # SimpleNamespace lets attribute access work too, but feedparser entries
    # are dict-like; we only rely on ``.get()`` in production code, so a plain
    # dict is closer to reality.
    ns = SimpleNamespace(**data)
    ns.get = data.get  # type: ignore[attr-defined]
    return ns


def _make_parsed(entries: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(entries=entries)


@asynccontextmanager
async def _fake_async_client(get_response):
    class _Client:
        async def get(self, url, *args, **kwargs):
            return get_response(url)

    yield _Client()


def _patch_yaml(monkeypatch, payload: dict[str, Any]) -> None:
    monkeypatch.setattr(news, "_config_cache", None)
    monkeypatch.setattr(news, "_load_config", lambda: payload, raising=True)


@pytest.fixture(autouse=True)
def _reset_news_state():
    news.reset_for_tests()
    yield
    news.reset_for_tests()


# ---------------------------------------------------------------------------
# Helpers — patching httpx.AsyncClient
# ---------------------------------------------------------------------------


def _patch_httpx_per_url(monkeypatch, response_factory) -> dict[str, Any]:
    """Replace ``news.httpx.AsyncClient`` with a stub keyed on URL."""
    captured: dict[str, Any] = {"urls": [], "init_kwargs": []}

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        captured["init_kwargs"].append(kwargs)

        class _Client:
            async def get(self, url, *_a, **_k):
                captured["urls"].append(url)
                return response_factory(url)

        yield _Client()

    monkeypatch.setattr(news.httpx, "AsyncClient", _client_factory)
    return captured


def _resp(body: bytes, status_code: int = 200):
    return SimpleNamespace(
        content=body,
        status_code=status_code,
        text=body.decode("utf-8", errors="ignore"),
    )


# ---------------------------------------------------------------------------
# (a) since filter
# ---------------------------------------------------------------------------


async def test_search_filters_by_since(monkeypatch):
    now = datetime.now(UTC)
    fresh = _make_entry(
        title="Fresh Story",
        link="https://example.com/fresh",
        summary="something happened",
        published=now - timedelta(days=1),
    )
    stale = _make_entry(
        title="Old Story",
        link="https://example.com/old",
        summary="years ago",
        published=now - timedelta(days=30),
    )

    _patch_yaml(monkeypatch, {"news": {"x": {"rss": ["https://example.com/feed"]}}})
    _patch_httpx_per_url(monkeypatch, lambda url: _resp(b"<rss/>"))

    monkeypatch.setattr(news.feedparser, "parse", lambda raw: _make_parsed([fresh, stale]))

    results = await news.search("", since=now - timedelta(days=7))

    urls = {r.url for r in results}
    assert "https://example.com/fresh" in urls
    assert "https://example.com/old" not in urls


async def test_search_keeps_entries_without_dates(monkeypatch):
    """Entries with no parseable date pass the since filter."""
    undated = _make_entry(
        title="Dateless",
        link="https://example.com/u",
        summary="no date here",
    )
    _patch_yaml(monkeypatch, {"news": {"x": {"rss": ["https://example.com/feed"]}}})
    _patch_httpx_per_url(monkeypatch, lambda url: _resp(b"<rss/>"))
    monkeypatch.setattr(news.feedparser, "parse", lambda raw: _make_parsed([undated]))

    results = await news.search("", since=datetime.now(UTC) - timedelta(days=1))
    assert len(results) == 1


# ---------------------------------------------------------------------------
# (b) substring/lowercase query filter
# ---------------------------------------------------------------------------


async def test_search_filters_by_query_substring(monkeypatch):
    matching = _make_entry(
        title="Federal Reserve hikes rates",
        link="https://example.com/fed",
        summary="powell announced ...",
    )
    other = _make_entry(
        title="Cats now sentient",
        link="https://example.com/cats",
        summary="local researchers stunned",
    )
    via_summary = _make_entry(
        title="Markets move",
        link="https://example.com/m",
        summary="The federal reserve made an announcement.",
    )

    _patch_yaml(monkeypatch, {"news": {"x": {"rss": ["https://e.com/feed"]}}})
    _patch_httpx_per_url(monkeypatch, lambda url: _resp(b"<rss/>"))
    monkeypatch.setattr(
        news.feedparser, "parse", lambda raw: _make_parsed([matching, other, via_summary])
    )

    results = await news.search("FEDERAL reserve")
    urls = {r.url for r in results}
    assert urls == {"https://example.com/fed", "https://example.com/m"}


# ---------------------------------------------------------------------------
# (c) one failing feed does not block others
# ---------------------------------------------------------------------------


async def test_one_failed_feed_does_not_block_others(monkeypatch, caplog):
    good_entry = _make_entry(
        title="Working feed",
        link="https://good.example/a",
    )

    def _factory(url: str):
        if "bad" in url:
            raise news.httpx.ConnectError("nope")
        return _resp(b"<rss/>")

    _patch_yaml(
        monkeypatch,
        {
            "news": {
                "x": {
                    "rss": [
                        "https://bad.example/feed",
                        "https://good.example/feed",
                    ]
                }
            }
        },
    )
    _patch_httpx_per_url(monkeypatch, _factory)
    monkeypatch.setattr(news.feedparser, "parse", lambda raw: _make_parsed([good_entry]))

    with caplog.at_level(logging.WARNING, logger=news.logger.name):
        results = await news.search("")

    assert [r.url for r in results] == ["https://good.example/a"]
    assert any("bad.example" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# (d) per-feed timeout
# ---------------------------------------------------------------------------


async def test_per_feed_timeout(monkeypatch, caplog):
    _patch_yaml(monkeypatch, {"news": {"x": {"rss": ["https://slow.example/feed"]}}})

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, url, *_a, **_k):
                raise TimeoutError("simulated 11s wait")

        yield _Client()

    monkeypatch.setattr(news.httpx, "AsyncClient", _client_factory)
    monkeypatch.setattr(news.feedparser, "parse", lambda raw: _make_parsed([]))

    with caplog.at_level(logging.WARNING, logger=news.logger.name):
        results = await news.search("")

    assert results == []
    assert any("slow.example" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# (e) scrape fallback extracts items
# ---------------------------------------------------------------------------


class _FakeLocator:
    def __init__(
        self,
        *,
        text: str = "",
        attrs: dict[str, str] | None = None,
        children: dict[str, _FakeLocator] | None = None,
        items: list[_FakeLocator] | None = None,
    ) -> None:
        self._text = text
        self._attrs = attrs or {}
        self._children = children or {}
        self._items = items or []

    @property
    def first(self) -> _FakeLocator:
        return self

    async def all(self) -> list[_FakeLocator]:
        return list(self._items)

    async def inner_text(self) -> str:
        return self._text

    async def get_attribute(self, name: str) -> str | None:
        return self._attrs.get(name)

    def locator(self, selector: str) -> _FakeLocator:
        return self._children.get(selector, _FakeLocator())


def _fake_item(*, title: str, href: str, summary: str = "") -> _FakeLocator:
    return _FakeLocator(
        children={
            "h2": _FakeLocator(text=title),
            "a": _FakeLocator(text=title, attrs={"href": href}),
            "p.summary": _FakeLocator(text=summary),
        }
    )


class _FakePage:
    def __init__(self, root: _FakeLocator) -> None:
        self._root = root
        self.closed = False
        self.screenshots: list[str] = []

    def locator(self, selector: str) -> _FakeLocator:
        return self._root

    async def close(self) -> None:
        self.closed = True

    async def screenshot(self, *, path: str) -> None:
        self.screenshots.append(path)


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self._page = page

    async def new_page(self) -> _FakePage:
        return self._page


def _stub_browser(monkeypatch, page: _FakePage) -> None:
    @asynccontextmanager
    async def _session(headful=None, block_media=True):
        yield _FakeContext(page)

    async def _navigate(p, url, **kwargs):
        return None

    monkeypatch.setattr(news.browser, "browser_session", _session)
    monkeypatch.setattr(news.browser, "navigate", _navigate)


async def test_scrape_fallback_extracts_items(monkeypatch):
    items = _FakeLocator(
        items=[
            _fake_item(
                title="Federal Reserve raises rates",
                href="/story/fed",
                summary="long-form analysis",
            ),
            _fake_item(
                title="Sports update",
                href="https://other.example/sports",
                summary="not relevant",
            ),
        ]
    )
    page = _FakePage(items)
    _stub_browser(monkeypatch, page)

    _patch_yaml(
        monkeypatch,
        {
            "news": {
                "tech": {
                    "scrape": [
                        {
                            "name": "fake-site",
                            "index_url": "https://news.example/",
                            "item_selector": "article",
                            "title_selector": "h2",
                            "link_selector": "a",
                            "summary_selector": "p.summary",
                        }
                    ]
                }
            }
        },
    )

    results = await news.search("federal reserve")
    assert len(results) == 1
    hit = results[0]
    assert hit.url == "https://news.example/story/fed"
    assert hit.title == "Federal Reserve raises rates"
    assert hit.snippet == "long-form analysis"
    assert hit.extras["fetched_via"] == "scrape"
    assert hit.extras["source_label"] == "fake-site"
    assert page.closed is True


# ---------------------------------------------------------------------------
# (f) scrape failure logs WARN and screenshots
# ---------------------------------------------------------------------------


async def test_scrape_failure_logs_warn_and_screenshots(monkeypatch, caplog, tmp_path):
    class _BrokenPage(_FakePage):
        def locator(self, selector: str) -> _FakeLocator:
            raise RuntimeError("selector drift")

    page = _BrokenPage(_FakeLocator())
    _stub_browser(monkeypatch, page)
    monkeypatch.setattr(news, "_DIAGNOSTICS_DIR", tmp_path / "diagnostics")

    _patch_yaml(
        monkeypatch,
        {
            "news": {
                "general": {
                    "scrape": [
                        {
                            "name": "broken-site",
                            "index_url": "https://broken.example/",
                            "item_selector": "article",
                            "title_selector": "h2",
                            "link_selector": "a",
                        }
                    ]
                }
            }
        },
    )

    with caplog.at_level(logging.WARNING, logger=news.logger.name):
        results = await news.search("anything")

    assert results == []
    assert any("broken" in rec.message.lower() for rec in caplog.records)
    assert page.screenshots, "expected at least one diagnostic screenshot path"
    assert page.screenshots[0].endswith(".png")
    assert page.closed is True


# ---------------------------------------------------------------------------
# (g) NEWSCATCHER cleansing — regression
# ---------------------------------------------------------------------------


def test_no_newscatcher_referenced_in_news_module():
    src = Path(news.__file__).read_text(encoding="utf-8")
    assert "NEWSCATCHER" not in src.upper()
    assert "newscatcher" not in src.lower()


def test_no_newscatcher_referenced_in_sources_yaml():
    cfg_path = Path("config/sources.yaml")
    if not cfg_path.exists():
        pytest.skip("config/sources.yaml not present in this checkout")
    text = cfg_path.read_text(encoding="utf-8")
    assert "NEWSCATCHER" not in text.upper()


# ---------------------------------------------------------------------------
# Default since window
# ---------------------------------------------------------------------------


async def test_default_since_is_seven_days(monkeypatch):
    captured: dict[str, datetime] = {}

    async def _fake(url, *, query, since):  # noqa: ARG001
        captured["since"] = since
        return []

    monkeypatch.setattr(news, "_fetch_rss", _fake)
    _patch_yaml(monkeypatch, {"news": {"x": {"rss": ["https://e.com/feed"]}}})

    await news.search("")
    diff = datetime.now(UTC) - captured["since"]
    # 7 days ± a small slack for clock progression during the test.
    assert timedelta(days=7) - diff < timedelta(seconds=5)


# ---------------------------------------------------------------------------
# Smoke registration
# ---------------------------------------------------------------------------


def test_smoke_registry_includes_news():
    from research_agent.tools import TOOL_REGISTRY

    assert "news" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["news"])


# ---------------------------------------------------------------------------
# Bundle selection
# ---------------------------------------------------------------------------


async def test_bundle_selects_single_bundle(monkeypatch):
    politics_calls: list[str] = []
    business_calls: list[str] = []

    async def _fake(url, *, query, since):  # noqa: ARG001
        if "politics" in url:
            politics_calls.append(url)
        else:
            business_calls.append(url)
        return []

    monkeypatch.setattr(news, "_fetch_rss", _fake)
    _patch_yaml(
        monkeypatch,
        {
            "news": {
                "politics": {"rss": ["https://e.com/politics"]},
                "business": {"rss": ["https://e.com/business"]},
            }
        },
    )

    await news.search("", bundle="politics")
    assert politics_calls and not business_calls
