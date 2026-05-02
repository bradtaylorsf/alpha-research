"""Tests for `research_agent.tools.reddit` (issue #21)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from research_agent.tools import reddit

# ---------------------------------------------------------------------------
# Fake Playwright objects
# ---------------------------------------------------------------------------


class _FakeLocator:
    """Minimal Playwright locator stand-in.

    Constructed with optional ``text``, ``attrs``, child locator map, and
    a list of items returned by ``.all()``. ``.first`` returns ``self`` so
    ``locator(sel).first`` chains work just like the real API.
    """

    def __init__(
        self,
        *,
        text: str = "",
        attrs: dict[str, str] | None = None,
        children: dict[str, _FakeLocator] | None = None,
        items: list[_FakeLocator] | None = None,
        raise_on_inner_text: bool = False,
    ) -> None:
        self._text = text
        self._attrs = attrs or {}
        self._children = children or {}
        self._items = items or []
        self._raise_on_inner_text = raise_on_inner_text

    @property
    def first(self) -> _FakeLocator:
        return self

    async def all(self) -> list[_FakeLocator]:
        return list(self._items)

    async def inner_text(self) -> str:
        if self._raise_on_inner_text:
            raise RuntimeError("inner_text boom")
        return self._text

    async def get_attribute(self, name: str) -> str | None:
        return self._attrs.get(name)

    def locator(self, selector: str) -> _FakeLocator:
        return self._children.get(selector, _FakeLocator())


class _FakePage:
    """Tracks navigations, supports custom selector responses, screenshots."""

    def __init__(self, selector_map: dict[str, _FakeLocator] | None = None) -> None:
        self._selector_map = selector_map or {}
        self.closed = False
        self.screenshots: list[str] = []

    def locator(self, selector: str) -> _FakeLocator:
        return self._selector_map.get(selector, _FakeLocator())

    async def close(self) -> None:
        self.closed = True

    async def screenshot(self, *, path: str) -> None:
        self.screenshots.append(path)


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self._page = page

    async def new_page(self) -> _FakePage:
        return self._page


def _stub_browser(monkeypatch, page: _FakePage) -> dict[str, Any]:
    captured: dict[str, Any] = {"navigations": []}

    @asynccontextmanager
    async def _session(headful=None, block_media=True):
        yield _FakeContext(page)

    async def _navigate(p, url, **kwargs):
        captured["navigations"].append(url)
        return None

    monkeypatch.setattr(reddit.browser, "browser_session", _session)
    monkeypatch.setattr(reddit.browser, "navigate", _navigate)
    return captured


def _make_search_item(
    *,
    title: str,
    href: str,
    snippet: str = "",
    subreddit: str = "",
    score: str = "",
    num_comments: str = "",
    posted_at: str = "",
) -> _FakeLocator:
    """Build a fake ``div.search-result-link`` matching what reddit.py reads."""
    children = {
        "a.search-title": _FakeLocator(text=title, attrs={"href": href}),
        "a.title": _FakeLocator(),
        ".search-result-body": _FakeLocator(text=snippet),
        ".md": _FakeLocator(),
        "a.search-subreddit-link": _FakeLocator(text=subreddit),
        ".search-score": _FakeLocator(text=score),
        ".score.unvoted": _FakeLocator(),
        ".search-comments": _FakeLocator(text=num_comments),
        "a.comments": _FakeLocator(),
        "time": _FakeLocator(attrs={"datetime": posted_at}),
    }
    return _FakeLocator(children=children, attrs={"data-subreddit": subreddit})


# ---------------------------------------------------------------------------
# (a) /search URL with query+sort
# ---------------------------------------------------------------------------


async def test_search_builds_global_search_url(monkeypatch):
    page = _FakePage(
        {
            "div.search-result-link": _FakeLocator(items=[]),
            "div.thing.link": _FakeLocator(items=[]),
        }
    )
    captured = _stub_browser(monkeypatch, page)

    # Empty-query path: no WARN even if zero results
    await reddit.search("", sort="new")

    assert captured["navigations"] == ["https://old.reddit.com/search?q=&sort=new"]


# ---------------------------------------------------------------------------
# (b) /r/<sub>/search URL with restrict_sr
# ---------------------------------------------------------------------------


async def test_search_with_subreddit_builds_restrict_sr_url(monkeypatch):
    page = _FakePage(
        {
            "div.search-result-link": _FakeLocator(items=[]),
            "div.thing.link": _FakeLocator(items=[]),
        }
    )
    captured = _stub_browser(monkeypatch, page)

    await reddit.search("qwen", subreddit="LocalLLaMA", sort="relevance")

    assert captured["navigations"] == [
        "https://old.reddit.com/r/LocalLLaMA/search?q=qwen&restrict_sr=on&sort=relevance"
    ]


# ---------------------------------------------------------------------------
# (c) parses title/url/subreddit/score/num_comments/posted_at
# ---------------------------------------------------------------------------


async def test_search_extracts_full_fields(monkeypatch):
    item = _make_search_item(
        title="Qwen 2.5 release notes",
        href="/r/LocalLLaMA/comments/abc/qwen_25/",
        snippet="long-form analysis",
        subreddit="LocalLLaMA",
        score="42 points",
        num_comments="11 comments",
        posted_at="2026-04-30T12:34:56+00:00",
    )
    page = _FakePage(
        {
            "div.search-result-link": _FakeLocator(items=[item]),
            "div.thing.link": _FakeLocator(items=[]),
        }
    )
    _stub_browser(monkeypatch, page)

    results = await reddit.search("qwen")
    assert len(results) == 1
    hit = results[0]
    assert hit.title == "Qwen 2.5 release notes"
    assert hit.url == "https://old.reddit.com/r/LocalLLaMA/comments/abc/qwen_25/"
    assert hit.snippet == "long-form analysis"
    assert hit.source_kind == "reddit"
    assert hit.extras["subreddit"] == "LocalLLaMA"
    assert hit.extras["score"] == 42
    assert hit.extras["num_comments"] == 11
    assert hit.extras["sort"] == "relevance"
    assert hit.extras["fetched_via"] == "old.reddit.com"
    assert hit.published_at is not None
    assert hit.published_at.year == 2026
    assert hit.published_at.month == 4
    assert hit.published_at.day == 30


# ---------------------------------------------------------------------------
# (c2) limit caps returned results
# ---------------------------------------------------------------------------


async def test_search_respects_limit(monkeypatch):
    items = [_make_search_item(title=f"Post {i}", href=f"/p/{i}", subreddit="x") for i in range(10)]
    page = _FakePage(
        {
            "div.search-result-link": _FakeLocator(items=items),
            "div.thing.link": _FakeLocator(items=[]),
        }
    )
    _stub_browser(monkeypatch, page)

    results = await reddit.search("anything", limit=3)
    assert len(results) == 3


# ---------------------------------------------------------------------------
# (c3) falls back to div.thing.link selector
# ---------------------------------------------------------------------------


async def test_search_falls_back_to_thing_link_selector(monkeypatch):
    item = _make_search_item(
        title="Listing-style",
        href="/r/x/comments/zzz/listing/",
        subreddit="x",
    )
    page = _FakePage(
        {
            "div.search-result-link": _FakeLocator(items=[]),
            "div.thing.link": _FakeLocator(items=[item]),
        }
    )
    _stub_browser(monkeypatch, page)

    results = await reddit.search("anything")
    assert len(results) == 1
    assert results[0].title == "Listing-style"


# ---------------------------------------------------------------------------
# (d) selector drift + non-empty query → 0 results, WARN, screenshot
# ---------------------------------------------------------------------------


async def test_selector_drift_logs_warn_and_screenshots(monkeypatch, caplog, tmp_path):
    page = _FakePage(
        {
            "div.search-result-link": _FakeLocator(items=[]),
            "div.thing.link": _FakeLocator(items=[]),
        }
    )
    _stub_browser(monkeypatch, page)
    monkeypatch.setattr(reddit, "_DIAGNOSTICS_DIR", tmp_path / "diagnostics")

    with caplog.at_level(logging.WARNING, logger=reddit.logger.name):
        results = await reddit.search("non empty query string")

    assert results == []
    warn_msgs = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warn_msgs) == 1
    assert "selector drift" in warn_msgs[0].message
    assert page.screenshots, "expected diagnostic screenshot saved"
    assert page.screenshots[0].endswith(".png")
    # Screenshot path lands under our patched diagnostics dir
    assert str(tmp_path / "diagnostics") in page.screenshots[0]


async def test_empty_query_zero_results_is_silent(monkeypatch, caplog):
    page = _FakePage(
        {
            "div.search-result-link": _FakeLocator(items=[]),
            "div.thing.link": _FakeLocator(items=[]),
        }
    )
    _stub_browser(monkeypatch, page)

    with caplog.at_level(logging.WARNING, logger=reddit.logger.name):
        results = await reddit.search("")

    assert results == []
    assert not [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert page.screenshots == []


# ---------------------------------------------------------------------------
# (e) fetch returns Source with body + depth-1 comments
# ---------------------------------------------------------------------------


def _make_comment(*, author: str, body: str, classes: str = "thing comment") -> _FakeLocator:
    return _FakeLocator(
        attrs={"class": classes},
        children={
            "a.author": _FakeLocator(text=author),
            ".usertext-body .md": _FakeLocator(text=body),
        },
    )


async def test_fetch_returns_source_with_comments(monkeypatch):
    page = _FakePage(
        {
            "a.title": _FakeLocator(text="Post title"),
            ".expando .md": _FakeLocator(text="Body of the post"),
            "div.usertext-body .md": _FakeLocator(),
            "a.subreddit": _FakeLocator(text="LocalLLaMA"),
            ".sitetable .score.unvoted": _FakeLocator(text="99"),
            ".sitetable a.comments": _FakeLocator(text="2 comments"),
            "div.commentarea > div.sitetable > div.thing.comment": _FakeLocator(
                items=[
                    _make_comment(author="alice", body="great post"),
                    _make_comment(author="bob", body="agreed"),
                    _make_comment(
                        author="",
                        body="should be skipped",
                        classes="thing morechildren",
                    ),
                ]
            ),
        }
    )
    captured = _stub_browser(monkeypatch, page)

    source = await reddit.fetch("https://www.reddit.com/r/LocalLLaMA/comments/abc/post/")
    assert source is not None
    assert source.url == "https://old.reddit.com/r/LocalLLaMA/comments/abc/post/"
    assert captured["navigations"] == ["https://old.reddit.com/r/LocalLLaMA/comments/abc/post/"]
    assert source.title == "Post title"
    assert source.source_kind == "reddit"
    assert "Body of the post" in source.cleaned_text
    assert "alice: great post" in source.cleaned_text
    assert "bob: agreed" in source.cleaned_text
    # ``.morechildren`` placeholder skipped — no body for it
    assert "should be skipped" not in source.cleaned_text
    assert "---" in source.cleaned_text  # body/comment separator
    assert source.metadata["subreddit"] == "LocalLLaMA"
    assert source.metadata["score"] == 99
    assert source.metadata["comment_count"] == 2
    assert page.closed is True


async def test_fetch_missing_title_and_body_returns_none(monkeypatch, tmp_path):
    page = _FakePage(
        {
            "a.title": _FakeLocator(text=""),
            ".expando .md": _FakeLocator(text=""),
            "div.usertext-body .md": _FakeLocator(text=""),
            "div.commentarea > div.sitetable > div.thing.comment": _FakeLocator(items=[]),
        }
    )
    _stub_browser(monkeypatch, page)
    monkeypatch.setattr(reddit, "_DIAGNOSTICS_DIR", tmp_path / "diagnostics")

    source = await reddit.fetch("https://old.reddit.com/r/x/comments/zzz/empty/")
    assert source is None
    assert page.screenshots, "expected diagnostic screenshot on empty post"


# ---------------------------------------------------------------------------
# (f) regression: no REDDIT_* env vars referenced
# ---------------------------------------------------------------------------


def test_no_reddit_env_vars_referenced_in_source():
    src = Path(reddit.__file__).read_text(encoding="utf-8")
    assert "REDDIT_CLIENT_ID" not in src
    assert "REDDIT_CLIENT_SECRET" not in src
    assert "REDDIT_USER_AGENT" not in src


# ---------------------------------------------------------------------------
# (g) smoke registry includes 'reddit'
# ---------------------------------------------------------------------------


def test_smoke_registry_includes_reddit():
    from research_agent.tools import TOOL_REGISTRY

    assert "reddit" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["reddit"])


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


async def test_search_failure_does_not_crash(monkeypatch):
    @asynccontextmanager
    async def _session(headful=None, block_media=True):
        raise RuntimeError("playwright went boom")
        yield  # pragma: no cover

    monkeypatch.setattr(reddit.browser, "browser_session", _session)

    results = await reddit.search("anything")
    assert results == []


def test_module_constants_match_polite_budget():
    # 1 nav / 2 s == 0.5 rps. Locking the constant in via test ensures
    # future drift is intentional, not accidental.
    assert reddit._HOST == "old.reddit.com"
    assert pytest.approx(reddit._HOST_RPS, rel=1e-6) == 0.5

    # Calling set_host_rate again is idempotent and registers the bucket
    # at the documented rate.
    reddit.browser.set_host_rate(reddit._HOST, reddit._HOST_RPS)
    bucket = reddit.browser._host_buckets.get(reddit._HOST)
    assert bucket is not None
    assert pytest.approx(bucket.rps, rel=1e-6) == reddit._HOST_RPS
