"""Tool/connector implementations: search, fetch, corpus, GitHub, arXiv, news, reddit, archive."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from research_agent.tools.models import SearchResult, Source, SourceKind


def _smoke_web_search(query: str) -> list[SearchResult]:
    """Smoke wrapper: run DDG then Google, return top-5 hits from each combined.

    The CLI smoke verb calls this synchronously and prints ``repr`` of the
    return value, so the output shows results from both engines per AC #14.
    """
    from research_agent.tools import web_search

    async def _run() -> list[SearchResult]:
        ddg = await web_search.search(query, max_results=5, engine="ddg")
        google = await web_search.search(query, max_results=5, engine="google")
        return ddg + google

    return asyncio.run(_run())


def _smoke_local_corpus(corpus_path: str) -> str:
    """Smoke wrapper: index ``corpus_path`` into a one-shot smoke job and report counts.

    Creates a fresh job under ``jobs/`` whose goal references the corpus
    path so repeated runs use distinct job ids (avoiding ``FileExistsError``
    when re-running the same smoke command in a clean repo).
    """
    from datetime import UTC, datetime

    from research_agent.storage import db
    from research_agent.storage.jobs import Job
    from research_agent.tools import local_corpus

    db.migrate().close()

    stamp = datetime.now(UTC).strftime("%H%M%S")
    intake = {"goal": f"smoke local_corpus {stamp}", "domain": "smoke"}
    job = Job.create(intake)
    summary = local_corpus.index(corpus_path, job)
    return (
        f"job: {job.id}\n"
        f"corpus: {corpus_path}\n"
        f"files_indexed: {summary['files_indexed']}\n"
        f"files_skipped: {summary['files_skipped']}\n"
        f"chunks_indexed: {summary['chunks_indexed']}\n"
        f"chunks_skipped: {summary['chunks_skipped']}\n"
        f"embed_dim: {summary['embed_dim']}"
    )


def _smoke_arxiv(query: str) -> str:
    """Smoke wrapper: arXiv search returning the top-5 hits as plain text.

    Each hit shows ``title``, abs URL, and a 200-char snippet of the abstract
    so AC #5 (`research _smoke-tool arxiv "..."` returns top 5 with abstracts)
    can be eyeballed against live data.
    """
    from research_agent.tools import arxiv_tool

    async def _run() -> str:
        results = await arxiv_tool.search(query, max_results=5)
        if not results:
            return f"arxiv search returned no results for {query!r}"
        lines: list[str] = []
        for hit in results:
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(f"- {hit.title}\n  {hit.url}\n  {snippet}")
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_edgar(query: str) -> str:
    """Smoke wrapper: EDGAR full-text search filtered to recent 8-K filings.

    Per AC #5: ``research _smoke-tool edgar "Cisco 8-K cybersecurity"`` should
    surface recent material-event filings. Each line shows the form, company,
    permalink, file date, and a short snippet so an operator can eyeball
    whether the SEC FTS index is reachable and the UA gate is healthy.
    """
    from research_agent.tools import edgar

    async def _run() -> str:
        results = await edgar.search(query, form_type="8-K", max_results=5)
        if not results:
            return f"edgar search returned no results for {query!r}"
        lines: list[str] = []
        for hit in results:
            company = hit.extras.get("company") or "?"
            form = hit.extras.get("form") or "?"
            file_date = (
                hit.published_at.date().isoformat() if hit.published_at else "?"
            )
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(
                f"- {form} {company}\n  {hit.url}\n  {file_date} {snippet}"
            )
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_courtlistener(query: str) -> str:
    """Smoke wrapper: CourtListener opinions search returning the top-5 hits.

    Per AC #6: ``research _smoke-tool courtlistener "first amendment retaliation"``
    should surface federal court opinions. Each line shows the case name, court,
    file date, citation, permalink, and a short snippet so an operator can
    eyeball whether the REST API is reachable and the token is healthy.
    """
    from research_agent.tools import courtlistener

    async def _run() -> str:
        results = await courtlistener.search(
            query, kind="opinions", max_results=5
        )
        if not results:
            return f"courtlistener search returned no results for {query!r}"
        lines: list[str] = []
        for hit in results:
            court = hit.extras.get("court") or "?"
            file_date = (
                hit.published_at.date().isoformat() if hit.published_at else "?"
            )
            citation = hit.extras.get("citation") or "?"
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(
                f"- {hit.title} — {court} — {file_date} — {citation}\n"
                f"  {hit.url}\n"
                f"  {snippet}"
            )
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_fedregister(query: str) -> str:
    """Smoke wrapper: Federal Register search returning the top-5 hits.

    Per AC #5: ``research _smoke-tool fedregister "AI executive order"`` should
    surface federal rules / proposed rules / notices. Each line shows the
    title, agencies, document type, publication date, significant flag,
    permalink, and a short abstract snippet so an operator can eyeball
    whether the Federal Register API is reachable.
    """
    from research_agent.tools import fedregister

    async def _run() -> str:
        results = await fedregister.search(query, max_results=5)
        if not results:
            return f"fedregister search returned no results for {query!r}"
        lines: list[str] = []
        for hit in results:
            agencies = ", ".join(hit.extras.get("agencies") or []) or "?"
            doc_type = hit.extras.get("document_type") or "?"
            pub_date = (
                hit.published_at.date().isoformat() if hit.published_at else "?"
            )
            significant = hit.extras.get("significant")
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(
                f"- {hit.title} — {agencies} — {doc_type} — {pub_date} —"
                f" significant={significant}\n"
                f"  {hit.url}\n"
                f"  {snippet}"
            )
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_nonprofits(query: str) -> str:
    """Smoke wrapper: ProPublica Nonprofit Explorer search returning the top-5 hits.

    Per AC: ``research _smoke-tool nonprofits "Heritage Foundation"`` should
    surface 501(c) organizations. Each line shows the org name, EIN,
    city/state, NTEE code, permalink, and snippet so an operator can
    eyeball whether the public API is reachable.
    """
    from research_agent.tools import nonprofits

    async def _run() -> str:
        results = await nonprofits.search(query, max_results=5)
        if not results:
            return f"nonprofits search returned no results for {query!r}"
        lines: list[str] = []
        for hit in results:
            ein = hit.extras.get("ein") or "?"
            city = hit.extras.get("city") or "?"
            state = hit.extras.get("state") or "?"
            ntee = hit.extras.get("ntee_code") or "?"
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(
                f"- {hit.title} — EIN {ein} — {city}, {state} — NTEE {ntee}\n"
                f"  {hit.url}\n"
                f"  {snippet}"
            )
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_fec(query: str) -> str:
    """Smoke wrapper: FEC OpenFEC candidate search + top-hit cycle totals.

    Per AC: ``research _smoke-tool fec "George Santos"`` should surface the
    candidate record + cycle totals. Lists the top-5 search hits, then
    follows up on the top hit with ``fetch()`` so an operator can eyeball
    receipts / disbursements / cash-on-hand alongside the candidate record.
    """
    from research_agent.tools import fec

    async def _run() -> str:
        results = await fec.search(query, kind="candidates", max_results=5)
        if not results:
            return f"fec search returned no results for {query!r}"
        lines: list[str] = []
        for hit in results:
            cand_id = hit.extras.get("candidate_id") or "?"
            party = hit.extras.get("party") or "?"
            state = hit.extras.get("state") or "?"
            office = hit.extras.get("office") or "?"
            cycles = hit.extras.get("election_years") or []
            cycles_str = ", ".join(str(c) for c in cycles) if cycles else "?"
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(
                f"- {hit.title} — {cand_id} — {party} — {state} — {office} —"
                f" cycles {cycles_str}\n"
                f"  {hit.url}\n"
                f"  {snippet}"
            )

        top = results[0]
        source = await fec.fetch(top.url)
        if source is None:
            lines.append(f"\nfetch({top.url}) returned None — cycle totals unavailable")
        else:
            totals = source.metadata.get("cycle_totals") or {}
            cycle = totals.get("cycle")
            header = f"\nCycle totals for {top.title} ({cycle})" if cycle else (
                f"\nCycle totals for {top.title}"
            )
            lines.append(header)
            lines.append(f"  receipts: {totals.get('receipts')}")
            lines.append(f"  disbursements: {totals.get('disbursements')}")
            lines.append(f"  cash_on_hand_end_period: {totals.get('cash_on_hand_end_period')}")
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_news(query: str) -> str:
    """Smoke wrapper: aggregate news hits and report per-source contributions.

    Runs ``news.search(query)`` over every configured bundle, prints the
    total count and a per-source breakdown grouped by ``fetched_via``
    (rss vs scrape), then the top-5 hits. Lets a human eyeball whether the
    RSS feeds and Playwright scrape recipes are still healthy without a
    full agent run.
    """
    from research_agent.tools import news

    async def _run() -> str:
        results = await news.search(query)
        if not results:
            return f"news search returned no results for {query!r}"

        groups: dict[str, dict[str, int]] = {"rss": {}, "scrape": {}}
        for hit in results:
            via = str(hit.extras.get("fetched_via") or "?")
            label = str(hit.extras.get("source_label") or "?")
            groups.setdefault(via, {})
            groups[via][label] = groups[via].get(label, 0) + 1

        lines: list[str] = [f"total: {len(results)}"]
        for via in ("rss", "scrape"):
            counts = groups.get(via, {})
            for label, count in sorted(counts.items()):
                lines.append(f"{via} {label}: {count}")

        for hit in results[:5]:
            published = hit.published_at.isoformat() if hit.published_at else "?"
            lines.append(f"- {hit.title}\n  {hit.url}\n  {published}")

        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_reddit(query: str) -> str:
    """Smoke wrapper: run reddit.search and print top hits formatted.

    Lets a human eyeball whether old.reddit.com selectors still parse
    cleanly without firing the full agent loop.
    """
    from research_agent.tools import reddit

    async def _run() -> str:
        results = await reddit.search(query)
        if not results:
            return f"reddit search returned no results for {query!r}"
        lines: list[str] = [f"total: {len(results)}"]
        for hit in results[:10]:
            sub = hit.extras.get("subreddit") or "?"
            score = hit.extras.get("score")
            num_comments = hit.extras.get("num_comments")
            lines.append(
                f"- {hit.title}\n  {hit.url}\n  r/{sub} | score={score} | comments={num_comments}"
            )
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_pdf(path_or_url: str) -> str:
    """Smoke wrapper for pdf.extract: print page-count summary + 500-char preview.

    Routes through :func:`pdf.extract_sync` so the verb works both for
    on-disk fixtures (smoke against ``tests/fixtures/arxiv_paper.pdf``) and
    HTTP URLs (smoke against an EDGAR 10-K).
    """
    from research_agent.tools import pdf

    text = pdf.extract_sync(path_or_url)
    if not text:
        return f"pdf extract returned empty markdown for {path_or_url}"
    page_headings = text.count("## Page ")
    preview = text[:500].replace("\n", " ")
    if len(text) > 500:
        preview += "…"
    return (
        f"source: {path_or_url}\n"
        f"page_sections: {page_headings}\n"
        f"char_count: {len(text)}\n"
        f"preview: {preview}"
    )


def _smoke_audio(path_or_url: str) -> str:
    """Smoke wrapper for audio.transcribe: chunk count + 500-char preview.

    Routes through :func:`audio.transcribe_sync` so it works for on-disk
    fixtures (a local ``.wav`` / ``.mp3``) and for HTTPS podcast URLs.
    """
    from research_agent.tools import audio

    text = audio.transcribe_sync(path_or_url)
    if not text:
        return f"audio transcribe returned empty markdown for {path_or_url}"
    chunks = text.count("## Chunk ")
    preview = text[:500].replace("\n", " ")
    if len(text) > 500:
        preview += "…"
    return (
        f"source: {path_or_url}\n"
        f"chunks: {chunks}\n"
        f"char_count: {len(text)}\n"
        f"preview: {preview}"
    )


def _smoke_ocr(path_or_url: str) -> str:
    """Smoke wrapper for ocr.extract: print source / char_count / preview.

    Routes through :func:`ocr.extract_sync` so the verb works for on-disk
    fixtures (``tests/fixtures/screenshot.png``) and HTTPS image URLs.
    """
    from research_agent.tools import ocr

    text = ocr.extract_sync(path_or_url)
    if not text:
        return f"ocr extract returned empty markdown for {path_or_url}"
    preview = text[:500].replace("\n", " ")
    if len(text) > 500:
        preview += "…"
    return (
        f"source: {path_or_url}\n"
        f"char_count: {len(text)}\n"
        f"preview: {preview}"
    )


def _smoke_youtube(query: str) -> str:
    """Smoke wrapper: run youtube.search and print up to 10 hits formatted.

    Lets a human eyeball whether the Data API key (when set) or the SERP
    fallback parser still picks up videos for ``query``.
    """
    from research_agent.tools import youtube

    async def _run() -> str:
        results = await youtube.search(query, max_results=10)
        if not results:
            return f"youtube search returned no results for {query!r}"
        lines: list[str] = [f"total: {len(results)}"]
        for hit in results[:10]:
            channel = hit.extras.get("channel") or "?"
            published = (
                hit.published_at.isoformat()
                if hit.published_at
                else (hit.extras.get("published_text") or "?")
            )
            views = hit.extras.get("view_count_text") or "?"
            lines.append(
                f"- {hit.title}\n  {hit.url}\n  {channel} | views={views} | published={published}"
            )
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_web_fetch(url: str) -> str:
    """Smoke wrapper for web_fetch: print word count, path, preview, archive URL.

    Returns a multi-line plain string so the CLI can print it verbatim.
    """
    from research_agent.tools import web_fetch

    async def _run() -> str:
        source = await web_fetch.fetch(url)
        if source is None:
            return f"web_fetch returned None for {url}"
        # Best-effort: give the archive task a brief window to finish so the
        # smoke output shows the URL when SPN is fast. We don't wait long;
        # callers in the live agent never block on archive at all.
        await asyncio.sleep(0)

        text = source.cleaned_text or ""
        word_count = len(text.split())
        preview = text[:200].replace("\n", " ")
        if len(text) > 200:
            preview += "…"
        fetched_via = source.metadata.get("fetched_via", "?")
        status = source.metadata.get("status_code")
        return (
            f"url: {source.url}\n"
            f"title: {source.title}\n"
            f"fetched_via: {fetched_via}\n"
            f"status_code: {status}\n"
            f"word_count: {word_count}\n"
            f"archive_url: {source.archive_url}\n"
            f"preview: {preview}"
        )

    return asyncio.run(_run())


# Registered tool callables, keyed by the name `research _smoke-tool` and the
# orchestrator dispatch by. The smoke verb checks against this dict so the
# dispatch surface is in place from day one.
TOOL_REGISTRY: dict[str, Callable[[str], object]] = {
    "web_search": _smoke_web_search,
    "web_fetch": _smoke_web_fetch,
    "local_corpus": _smoke_local_corpus,
    "arxiv": _smoke_arxiv,
    "edgar": _smoke_edgar,
    "courtlistener": _smoke_courtlistener,
    "fedregister": _smoke_fedregister,
    "nonprofits": _smoke_nonprofits,
    "fec": _smoke_fec,
    "news": _smoke_news,
    "reddit": _smoke_reddit,
    "pdf": _smoke_pdf,
    "audio": _smoke_audio,
    "ocr": _smoke_ocr,
    "youtube": _smoke_youtube,
}

__all__ = ["TOOL_REGISTRY", "SearchResult", "Source", "SourceKind"]
