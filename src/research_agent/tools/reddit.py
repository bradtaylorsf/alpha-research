"""Reddit connector — JSON endpoint via httpx, no Playwright.

Earlier version used Playwright on ``old.reddit.com``; in practice the
scrape would block for ~10 minutes (and once for ~1 hour) on the same
queries the JSON endpoint returns in <1s. The legacy ``old.reddit.com``
HTML path is preserved upstream in git history if we ever need to bring
it back as a fallback.

Public surface (unchanged):

* ``async def search(query, subreddit=None, sort='relevance', limit=25)``
  — returns up to ``limit`` :class:`SearchResult` from
  ``reddit.com/search.json`` (or ``r/<sub>/search.json``).
* ``async def fetch(url)`` — fetches a post via its ``.json`` endpoint
  and returns a :class:`Source` whose ``cleaned_text`` is the body plus
  top-level comments.

Reddit's anonymous API requires a non-default User-Agent (the bare
``python-httpx`` UA is rate-limited or 429'd). We send the project's
configured ``RESEARCH_USER_AGENT`` (or a sensible default).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from urllib.parse import quote_plus, urlparse

import httpx

from research_agent import config
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

_DEFAULT_USER_AGENT = "research-agent/0.1 (+https://github.com/anthropics/research-agent)"
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT_S = 15.0
_PERMALINK_RE = re.compile(r"^/r/[^/]+/comments/[^/]+/[^/]+/?$")


def _user_agent() -> str:
    """Resolve the UA string Reddit will see.

    Reddit's anonymous JSON endpoint started 403'ing the project's
    descriptive UA (``research-agent/0.1 …``) — they want either an OAuth
    app or a browser UA. We send a Chrome UA as the default; an operator
    who needs a bespoke UA can set ``RESEARCH_REDDIT_USER_AGENT`` (or fall
    back to ``RESEARCH_USER_AGENT``) to override.
    """
    return (
        config.get("RESEARCH_REDDIT_USER_AGENT")
        or config.get("RESEARCH_USER_AGENT")
        or _BROWSER_USER_AGENT
    )


def _browser_headers() -> dict[str, str]:
    """Full set of headers Reddit's bot detection wants to see.

    Empirically: sending only ``User-Agent`` returns 403 with an HTML
    "blocked" page even when the UA matches Chrome. Adding ``Accept``,
    ``Accept-Language``, and ``Accept-Encoding`` (the headers a real
    browser always sends) flips the response to 200 JSON. Reddit's
    Cloudflare-style detector clearly checks the full header tuple, not
    just the UA.
    """
    return {
        "User-Agent": _user_agent(),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }


def _build_search_url(query: str, subreddit: str | None, sort: str, limit: int) -> str:
    q = quote_plus(query or "")
    sort_q = quote_plus(sort or "relevance")
    n = max(1, min(int(limit), 100))
    if subreddit:
        sub = subreddit.lstrip("/").removeprefix("r/")
        return (
            f"https://www.reddit.com/r/{sub}/search.json"
            f"?q={q}&restrict_sr=on&sort={sort_q}&limit={n}"
        )
    return f"https://www.reddit.com/search.json?q={q}&sort={sort_q}&limit={n}"


def _absolute_permalink(permalink: str) -> str:
    if permalink.startswith("http"):
        return permalink
    return "https://www.reddit.com" + permalink


def _parse_created_utc(value: object) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _post_to_search_result(post: dict) -> SearchResult | None:
    title = (post.get("title") or "").strip()
    permalink = (post.get("permalink") or "").strip()
    if not title or not permalink:
        return None
    snippet = (post.get("selftext") or "").strip()
    if len(snippet) > 400:
        snippet = snippet[:400] + "…"
    return SearchResult(
        url=_absolute_permalink(permalink),
        title=title,
        snippet=snippet,
        published_at=_parse_created_utc(post.get("created_utc")),
        source_kind="reddit",
        score=float(post["score"]) if isinstance(post.get("score"), (int, float)) else None,
        extras={
            "subreddit": post.get("subreddit"),
            "num_comments": post.get("num_comments"),
            "fetched_via": "reddit-json",
        },
    )


async def search(
    query: str,
    subreddit: str | None = None,
    sort: str = "relevance",
    limit: int = 25,
) -> list[SearchResult]:
    """Search Reddit for ``query``; return up to ``limit`` :class:`SearchResult`.

    Hits ``reddit.com/search.json`` (or ``r/<sub>/search.json`` when
    ``subreddit`` is given) and parses the listing payload. Anonymous —
    no OAuth, no API keys, no Playwright. Reddit's documented anonymous
    rate limit is ~60 req/min; the per-job task cadence is well under that.

    Returns ``[]`` on any HTTP/JSON error rather than raising — search
    failures should not abort the loop, the planner can route around them.
    """
    if not query or not query.strip():
        return []

    url = _build_search_url(query, subreddit, sort, limit)
    headers = _browser_headers()
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("reddit search HTTP error for %r: %s", query, exc)
        return []

    if resp.status_code != 200:
        logger.warning(
            "reddit search returned %s for %r: %s",
            resp.status_code,
            query,
            resp.text[:200],
        )
        return []

    try:
        data = resp.json()
    except ValueError as exc:
        logger.warning("reddit search JSON decode failed: %s", exc)
        return []

    children = ((data or {}).get("data") or {}).get("children") or []
    results: list[SearchResult] = []
    for child in children:
        post = (child or {}).get("data") or {}
        sr = _post_to_search_result(post)
        if sr is not None:
            results.append(sr)
    return results[:limit]


def _normalize_to_json_url(url: str) -> str | None:
    """Turn a reddit post permalink into its ``.json`` form, or None if not a post.

    Accepts ``www.reddit.com``, ``old.reddit.com``, or ``reddit.com`` hosts.
    Only the canonical ``/r/<sub>/comments/<id>/<slug>`` permalink shape is
    supported; anything else (subreddit listings, user pages) returns None.
    """
    parsed = urlparse(url)
    if parsed.netloc not in {"www.reddit.com", "old.reddit.com", "reddit.com", "new.reddit.com"}:
        return None
    path = parsed.path.rstrip("/")
    if not _PERMALINK_RE.match(path + "/"):
        return None
    return f"https://www.reddit.com{path}.json"


def _comment_body(node: dict) -> str | None:
    body = (node.get("body") or "").strip()
    if not body or body in {"[deleted]", "[removed]"}:
        return None
    return body


async def fetch(url: str) -> Source | None:
    """Fetch a Reddit post + its top-level comments as a :class:`Source`.

    The Reddit JSON endpoint at ``<permalink>.json`` returns
    ``[post_listing, comments_listing]``. We collapse the post body and
    every non-deleted top-level comment into a single markdown blob —
    sufficient context for ``extract_findings`` without pulling the full
    comment tree.
    """
    json_url = _normalize_to_json_url(url)
    if json_url is None:
        logger.warning("reddit fetch: not a post permalink: %s", url)
        return None

    headers = _browser_headers()
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.get(json_url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("reddit fetch HTTP error for %s: %s", url, exc)
        return None

    if resp.status_code != 200:
        logger.warning("reddit fetch returned %s for %s", resp.status_code, url)
        return None

    try:
        listings = resp.json()
    except ValueError as exc:
        logger.warning("reddit fetch JSON decode failed: %s", exc)
        return None

    if not isinstance(listings, list) or len(listings) < 1:
        return None

    post_listing = listings[0]
    children = ((post_listing or {}).get("data") or {}).get("children") or []
    if not children:
        return None
    post = (children[0] or {}).get("data") or {}
    title = (post.get("title") or "").strip()
    body = (post.get("selftext") or "").strip()

    comment_lines: list[str] = []
    if len(listings) >= 2:
        comments_listing = listings[1]
        comment_children = ((comments_listing or {}).get("data") or {}).get("children") or []
        for c in comment_children:
            data = (c or {}).get("data") or {}
            text = _comment_body(data)
            if text:
                comment_lines.append(f"- {text}")

    parts: list[str] = []
    if title:
        parts.append(f"# {title}")
    if body:
        parts.append(body)
    if comment_lines:
        parts.append("## Top-level comments\n\n" + "\n\n".join(comment_lines))

    cleaned = "\n\n".join(parts).strip()
    if not cleaned:
        return None

    return Source(
        url=_absolute_permalink(post.get("permalink") or url),
        title=title or url,
        cleaned_text=cleaned,
        fetched_at=datetime.now(UTC),
        source_kind="reddit",
        metadata={
            "subreddit": post.get("subreddit"),
            "score": post.get("score"),
            "num_comments": post.get("num_comments"),
            "fetched_via": "reddit-json",
        },
    )


__all__ = ["fetch", "search"]
