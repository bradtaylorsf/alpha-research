"""Tool/connector implementations: search, fetch, corpus, GitHub, arXiv, news, reddit, archive."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

# Import every direct-connector module so its module-level `register_kind`
# call runs and the kind shows up in `iter_kinds()`. The import order is
# alphabetical for readability, but the registry sorts on read so output
# stays deterministic regardless of import order. Issue #223: this is the
# single source of truth — the planner prompt, the orchestrator dispatcher,
# the README table, and `research doctor` all walk this list.
from research_agent.tools import (  # noqa: F401, E402 — side-effecting registration
    bbb,
    calaccess,
    commons,
    congress,
    courtlistener,
    edgar,
    fec,
    fedregister,
    gallica,
    gdelt,
    iarchive,
    lda,
    licensing,
    linkedin,
    littlesis,
    loc,
    nonprofits,
    opencorporates,
    openlibrary,
    persee,
    sanctions,
    scholar,
    sos,
    trove,
    usaspending,
    wikidata,
    wikisource,
)
from research_agent.tools._registry import (
    BaseSearchPayload,
    KindEntry,
    iter_kinds,
    register_kind,
)
from research_agent.tools.models import SearchResult, Source, SourceKind


def _smoke_web_search(query: str) -> str:
    """Smoke wrapper: exercise the runtime ``engine="auto"`` path.

    Mirrors what the orchestrator actually does: a single
    ``web_search.search(query, engine="auto")`` call. With
    ``BRAVE_SEARCH_API_KEY`` set, ``auto`` resolves to the Brave Search API;
    without the key, it falls back to DDG-Playwright (which is subject to
    selector drift and may legitimately return zero hits even when the
    runtime is healthy). The output line marks which engine actually ran so
    operators can tell apart a Brave miss from a Playwright fallback miss
    without re-running.
    """
    from research_agent.tools import web_search

    async def _run() -> str:
        results = await web_search.search(query, max_results=10, engine="auto")
        if results:
            engine_used = str(results[0].extras.get("source_engine") or "?")
        else:
            engine_used = "brave" if web_search._brave_api_key() else "ddg"

        if engine_used == "brave":
            header = f"engine=brave: returned {len(results)} hits"
        elif engine_used == "ddg":
            suffix = " (selector drift?)" if not results else ""
            header = (
                f"engine=ddg (Playwright fallback, subject to selector drift):"
                f" returned {len(results)} hits{suffix}"
            )
        else:
            header = f"engine={engine_used}: returned {len(results)} hits"

        lines: list[str] = [header]
        for hit in results:
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(f"- {hit.title}\n  {hit.url}\n  {snippet}")
        return "\n".join(lines)

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

    SEC requires a contact email in the User-Agent. When ``RESEARCH_USER_AGENT``
    is unset (or doesn't include ``@``), the production path raises
    ``RuntimeError`` — for smoke we'd rather skip gracefully so the
    per-issue smoke gate doesn't block unrelated work.
    """
    from research_agent import config

    ua = config.get("RESEARCH_USER_AGENT") or ""
    if "@" not in ua:
        return (
            "_smoke-tool edgar: would need RESEARCH_USER_AGENT"
            " (with contact email); live test skipped"
        )

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


def _smoke_scholar(query: str) -> str:
    """Smoke wrapper: SERPAPI Google Scholar (case law) returning the top-5 hits.

    Per AC: ``research _smoke-tool scholar "first amendment retaliation Ninth
    Circuit"`` should surface case-law hits. Each line shows the case name,
    court / journal summary, year, cited-by count, permalink, and snippet so
    an operator can eyeball whether the SERPAPI key is healthy and the
    Scholar engine is reachable.
    """
    from research_agent.tools import scholar

    async def _run() -> str:
        results = await scholar.search(query, kind="case_law", max_results=5)
        if not results:
            return f"scholar search returned no results for {query!r}"
        lines: list[str] = []
        for hit in results:
            court_or_journal = hit.extras.get("court_or_journal") or "?"
            year = (
                hit.published_at.year if hit.published_at is not None else "?"
            )
            cited_by = hit.extras.get("citation") or 0
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(
                f"- {hit.title} — {court_or_journal} — {year} — cited-by {cited_by}\n"
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
            return (
                f"fedregister search returned 0 results for {query!r}"
                " (valid query, no matching documents)"
            )
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


def _smoke_loc(query: str) -> str:
    """Smoke wrapper: Library of Congress unified search.

    Per AC: ``research _smoke-tool loc_search "battle of algiers"`` should
    return ≥1 result with a non-empty title and a ``www.loc.gov`` URL.
    Each line shows the title, URL, and source_kind so an operator can
    eyeball whether the loc.gov JSON API is reachable.
    """
    from research_agent.tools import loc

    async def _run() -> str:
        results = await loc.search(query, max_results=5)
        if not results:
            return f"loc search returned no results for {query!r}"
        lines: list[str] = []
        for hit in results:
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(
                f"- {hit.title} — source_kind={hit.source_kind}\n"
                f"  {hit.url}\n"
                f"  {snippet}"
            )
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_iarchive(query: str) -> str:
    """Smoke wrapper: Internet Archive advancedsearch returning the top-5 hits.

    Per AC: ``research _smoke-tool iarchive_search "Pullman Strike"`` should
    surface IA items with title, identifier, mediatype, downloads, permalink,
    and a short snippet so an operator can eyeball whether the public API
    is reachable.
    """
    from research_agent.tools import iarchive

    async def _run() -> str:
        results = await iarchive.search(query, max_results=5)
        if not results:
            return f"iarchive search returned no results for {query!r}"
        lines: list[str] = []
        for hit in results:
            identifier = hit.extras.get("identifier") or "?"
            mediatype = hit.extras.get("mediatype") or "?"
            downloads = hit.extras.get("downloads")
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(
                f"- {hit.title} — {identifier} — {mediatype} —"
                f" downloads={downloads}\n"
                f"  {hit.url}\n"
                f"  {snippet}"
            )
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_linkedin(query: str) -> str:
    """Smoke wrapper: LinkedIn person search + top-hit profile rollup.

    Per AC: ``research _smoke-tool linkedin "George Santos"`` should
    return the canonical profile via the configured broker (Proxycurl by
    default). Lists the top-5 person hits with current title / company /
    location / permalink, then attempts ``fetch()`` on the top hit and
    prints current title, current company, employment count, and
    education count so an operator can eyeball broker health.
    """
    from research_agent.tools import linkedin

    async def _run() -> str:
        results = await linkedin.search(query, kind="person", max_results=5)
        if not results:
            return f"linkedin search returned no results for {query!r}"
        lines: list[str] = []
        for hit in results:
            current_title = hit.extras.get("current_title") or "?"
            current_company = hit.extras.get("current_company") or "?"
            location = hit.extras.get("location") or "?"
            broker = hit.extras.get("broker") or "?"
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(
                f"- {hit.title} — {current_title} @ {current_company} —"
                f" {location} — broker {broker}\n"
                f"  {hit.url}\n"
                f"  {snippet}"
            )

        top = results[0]
        source = await linkedin.fetch(top.url)
        if source is None:
            lines.append(
                f"\nfetch({top.url}) returned None — broker call failed"
            )
        else:
            employment = source.metadata.get("employment_history") or []
            education = source.metadata.get("education") or []
            current_title = ""
            current_company = ""
            if employment:
                first = employment[0]
                if isinstance(first, dict):
                    current_title = str(first.get("title") or "")
                    current_company = str(first.get("company") or "")
            lines.append(f"\nProfile for {top.title}")
            lines.append(f"  current_title: {current_title or '?'}")
            lines.append(f"  current_company: {current_company or '?'}")
            lines.append(f"  employment_count: {len(employment)}")
            lines.append(f"  education_count: {len(education)}")
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


def _smoke_lda(query: str) -> str:
    """Smoke wrapper: LDA registrant search + recent quarterly filings.

    Per AC: ``research _smoke-tool lda "Heritage Foundation"`` should
    surface the registrant record + recent quarterly filings. Lists the
    top-5 registrant hits, then for the top hit lists the 5 most recent
    LD-1/LD-2 filings so an operator can eyeball whether the REST API is
    reachable and the optional API token (when set) is healthy.
    """
    from research_agent.tools import lda

    async def _run() -> str:
        registrants = await lda.search(query, kind="registrants", max_results=5)
        if not registrants:
            return f"lda search returned no registrants for {query!r}"
        lines: list[str] = ["# Registrants"]
        for hit in registrants:
            address = hit.extras.get("address") or "?"
            contact = hit.extras.get("contact") or "?"
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(
                f"- {hit.title}\n"
                f"  {address}\n"
                f"  Contact: {contact}\n"
                f"  {hit.url}\n"
                f"  {snippet}"
            )

        top = registrants[0]
        filings = await lda.search(top.title, kind="filings", max_results=5)
        lines.append(f"\n# Recent filings for {top.title}")
        if not filings:
            lines.append("(no filings returned)")
        else:
            for hit in filings:
                year = hit.extras.get("filing_year") or "?"
                period = hit.extras.get("filing_period") or "?"
                ftype = hit.extras.get("filing_type") or "?"
                income = hit.extras.get("income")
                expenses = hit.extras.get("expenses")
                lines.append(
                    f"- {hit.title}\n"
                    f"  {ftype} — {year} {period} — income={income} expenses={expenses}\n"
                    f"  {hit.url}"
                )
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_littlesis(query: str) -> str:
    """Smoke wrapper: LittleSis entity search + top-hit relationships.

    Per AC: ``research _smoke-tool littlesis "Peter Thiel"`` should surface
    the entity row plus a snapshot of who they're connected to. Lists the
    top-5 entity hits (name, primary_ext, permalink), then for the top hit
    fetches relationships and prints a count + first 5 categorised edges.

    LittleSis is user-contributed — output is a research lead, not evidence.
    """
    from research_agent.tools import littlesis

    async def _run() -> str:
        results = await littlesis.search(query, kind="entities", max_results=5)
        if not results:
            return f"littlesis search returned no results for {query!r}"
        lines: list[str] = ["# Entities"]
        for hit in results:
            primary_ext = hit.extras.get("primary_ext") or "?"
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(
                f"- {hit.title} — {primary_ext}\n"
                f"  {hit.url}\n"
                f"  {snippet}"
            )

        top = results[0]
        rels = await littlesis.search(top.title, kind="relationships", max_results=5)
        lines.append(f"\n# Relationships for {top.title}")
        if not rels:
            lines.append("(no relationships returned)")
        else:
            lines.append(f"total: {len(rels)}")
            for rel in rels[:5]:
                category = rel.extras.get("category_label") or "?"
                snippet = rel.snippet.replace("\n", " ")
                if len(snippet) > 200:
                    snippet = snippet[:200] + "…"
                lines.append(
                    f"- {rel.title}\n"
                    f"  [{category}] {snippet}\n"
                    f"  {rel.url}"
                )
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_opencorporates(query: str) -> str:
    """Smoke wrapper: OpenCorporates company search returning the top-5 hits.

    OpenCorporates removed anonymous v0.4 access (returns HTTP 401), so the
    smoke verb skips cleanly when ``OPENCORPORATES_API_KEY`` is unset. With
    a key, ``research _smoke-tool opencorporates "SBI Builders"`` should
    surface the California LLC entry alongside its registered agent.
    """
    from research_agent import config
    from research_agent.tools import opencorporates

    if not (config.get("OPENCORPORATES_API_KEY") or "").strip():
        return (
            "opencorporates: would need OPENCORPORATES_API_KEY;"
            " live test skipped"
        )

    async def _run() -> str:
        results = await opencorporates.search(query, max_results=5)
        if not results:
            return f"opencorporates search returned no results for {query!r}"
        lines: list[str] = []
        for hit in results:
            number = hit.extras.get("company_number") or "?"
            jurisdiction = hit.extras.get("jurisdiction_code") or "?"
            status = hit.extras.get("current_status") or "?"
            agent = hit.extras.get("registered_agent_name") or "?"
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(
                f"- {hit.title} — {number} — {jurisdiction} — {status} —"
                f" agent: {agent}\n"
                f"  {hit.url}\n"
                f"  {snippet}"
            )
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_trove_search(query: str) -> str:
    """Smoke wrapper: Trove metadata search, skipping when no API key is set."""
    from research_agent import config

    if not (config.get("TROVE_API_KEY") or "").strip():
        return (
            "_smoke-tool trove_search: skipped; would need TROVE_API_KEY "
            "(keys expire after 12 months; connector is metadata-only)"
        )

    from research_agent.tools import trove

    async def _run() -> str:
        results = await trove.search(query, max_results=5)
        if not results:
            raise RuntimeError(
                f"_smoke-tool trove_search: search({query!r}) returned 0 results"
            )
        lines: list[str] = [
            f"trove_search metadata-only: returned {len(results)} hits"
        ]
        for hit in results:
            trove_id = hit.extras.get("trove_id") or "?"
            zone = hit.extras.get("zone") or "?"
            pub_date = hit.extras.get("pub_date") or "?"
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 180:
                snippet = snippet[:180] + "..."
            lines.append(
                f"- {hit.title}\n"
                f"  url: {hit.url}\n"
                f"  trove_id: {trove_id} | zone: {zone} | pub_date: {pub_date}\n"
                f"  snippet: {snippet}"
            )
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_wikidata_search(query: str) -> str:
    """Smoke wrapper: raw SPARQL query against Wikidata Query Service."""
    import sys

    async def _run() -> str:
        results = await wikidata.search(query, max_results=10)
        if not results:
            print(
                f"_smoke-tool wikidata_search: search({query!r}) returned 0 results",
                file=sys.stderr,
            )
            raise SystemExit(1)
        lines = [f"wikidata_search: returned {len(results)} hits"]
        for hit in results:
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 240:
                snippet = snippet[:240] + "..."
            entity_id = hit.extras.get("entity_id") or "?"
            lines.append(f"- {hit.title} ({entity_id})\n  {hit.url}\n  {snippet}")
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_commons_search(query: str) -> str:
    """Smoke wrapper: Wikimedia Commons media search with required license metadata."""
    import sys

    async def _run() -> str:
        results = await commons.search(query, max_results=5)
        if not results:
            print(
                f"_smoke-tool commons_search: search({query!r}) returned 0 results",
                file=sys.stderr,
            )
            raise SystemExit(1)

        missing = [
            hit.title
            for hit in results
            if not hit.title or not hit.url or not hit.extras.get("license")
        ]
        if missing:
            print(
                "_smoke-tool commons_search: missing title/url/license on "
                f"{len(missing)} result(s): {missing[:3]}",
                file=sys.stderr,
            )
            raise SystemExit(1)

        source = await commons.fetch(results[0].url)
        if source is None or not source.metadata.get("license"):
            print(
                "_smoke-tool commons_search: fetch(top_hit) did not populate "
                'Source.metadata["license"]',
                file=sys.stderr,
            )
            raise SystemExit(1)

        lines = [f"commons_search: returned {len(results)} license-bearing hits"]
        for hit in results:
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "..."
            lines.append(
                f"- {hit.title}\n"
                f"  url: {hit.url}\n"
                f"  license: {hit.extras.get('license')} "
                f"({hit.extras.get('license_short') or '?'})\n"
                f"  mime_type: {hit.extras.get('mime_type') or '?'}\n"
                f"  snippet: {snippet}"
            )
        lines.append(
            f"fetched metadata.license: {source.metadata['license']}"
        )
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_wikisource_search(query: str) -> str:
    """Smoke wrapper: Wikisource search plus top-hit full-text fetch."""
    import sys

    async def _run() -> str:
        results = await wikisource.search(query, max_results=5)
        if not results:
            print(
                f"_smoke-tool wikisource_search: search({query!r}) returned 0 results",
                file=sys.stderr,
            )
            raise SystemExit(1)

        missing = [hit.title for hit in results if not hit.title or not hit.url]
        if missing:
            print(
                "_smoke-tool wikisource_search: missing title/url on "
                f"{len(missing)} result(s): {missing[:3]}",
                file=sys.stderr,
            )
            raise SystemExit(1)

        source = await wikisource.fetch(results[0].url)
        if (
            source is None
            or not source.cleaned_text.strip()
            or not source.metadata.get("wikisource_lang")
            or not source.metadata.get("page_title")
            or source.metadata.get("revision_id") in (None, "")
        ):
            print(
                "_smoke-tool wikisource_search: fetch(top_hit) did not populate "
                "cleaned_text and required Wikisource metadata",
                file=sys.stderr,
            )
            raise SystemExit(1)

        lines = [f"wikisource_search: returned {len(results)} hits"]
        for hit in results:
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "..."
            lines.append(
                f"- {hit.title}\n"
                f"  url: {hit.url}\n"
                f"  lang: {hit.extras.get('wikisource_lang') or '?'}\n"
                f"  snippet: {snippet}"
            )
        preview = source.cleaned_text.replace("\n", " ")
        if len(preview) > 240:
            preview = preview[:240] + "..."
        lines.append(
            "fetched: "
            f"title={source.title!r} "
            f"lang={source.metadata['wikisource_lang']} "
            f"revision_id={source.metadata['revision_id']} "
            f"chars={len(source.cleaned_text)}"
        )
        lines.append(f"preview: {preview}")
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_openlibrary_search(query: str) -> str:
    """Smoke wrapper: Open Library search with non-empty title/URL checks."""
    import sys

    async def _run() -> str:
        results = await openlibrary.search(query, max_results=5)
        if not results:
            print(
                f"_smoke-tool openlibrary_search: search({query!r}) returned 0 results",
                file=sys.stderr,
            )
            raise SystemExit(1)

        missing = [
            hit.title or hit.url for hit in results if not hit.title or not hit.url
        ]
        if missing:
            print(
                "_smoke-tool openlibrary_search: missing title/url on "
                f"{len(missing)} result(s): {missing[:3]}",
                file=sys.stderr,
            )
            raise SystemExit(1)

        lines = [f"openlibrary_search: returned {len(results)} hits"]
        for hit in results:
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "..."
            lines.append(
                f"- {hit.title}\n"
                f"  url: {hit.url}\n"
                f"  ia_scan_id: "
                f"{', '.join(hit.extras.get('ia_scan_id') or []) or '?'}\n"
                f"  isbn: {', '.join(hit.extras.get('isbn') or []) or '?'}\n"
                f"  oclc: {', '.join(hit.extras.get('oclc') or []) or '?'}\n"
                f"  snippet: {snippet}"
            )
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_gallica_search(query: str) -> str:
    """Smoke wrapper: Gallica SRU XML search with non-empty title/URL checks."""
    import sys

    async def _run() -> str:
        results = await gallica.search(query, max_results=5)
        if not results:
            print(
                f"_smoke-tool gallica_search: search({query!r}) returned 0 results",
                file=sys.stderr,
            )
            raise SystemExit(1)

        missing = [
            hit.title or hit.url
            for hit in results
            if (
                not hit.title
                or not hit.url
                or "gallica.bnf.fr/" not in hit.url
                or not hit.extras.get("ark")
            )
        ]
        if missing:
            print(
                "_smoke-tool gallica_search: missing title/gallica URL/ARK on "
                f"{len(missing)} result(s): {missing[:3]}",
                file=sys.stderr,
            )
            raise SystemExit(1)

        lines = [f"gallica_search: returned {len(results)} hits"]
        for hit in results:
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "..."
            lines.append(
                f"- {hit.title}\n"
                f"  url: {hit.url}\n"
                f"  ark: {hit.extras.get('ark')}\n"
                f"  date: {hit.extras.get('dc:date') or 'none listed'}\n"
                f"  language: {hit.extras.get('dc:language') or 'none listed'}\n"
                f"  snippet: {snippet}"
            )
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_persee_search(query: str) -> str:
    """Smoke wrapper: Persee Playwright search with non-empty title/URL checks."""
    import sys

    async def _run() -> str:
        results = await persee.search(query, max_results=5)
        if not results:
            print(
                f"_smoke-tool persee_search: search({query!r}) returned 0 results",
                file=sys.stderr,
            )
            raise SystemExit(1)

        missing = [
            hit.title or hit.url
            for hit in results
            if not hit.title
            or not hit.url
            or "persee.fr/" not in hit.url
            or not hit.url.startswith(("https://www.persee.fr/", "https://persee.fr/"))
        ]
        if missing:
            print(
                "_smoke-tool persee_search: missing title/persee.fr URL on "
                f"{len(missing)} result(s): {missing[:3]}",
                file=sys.stderr,
            )
            raise SystemExit(1)

        lines = [f"persee_search: returned {len(results)} hits"]
        for hit in results:
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "..."
            authors = hit.extras.get("authors")
            if isinstance(authors, list):
                authors_text = "; ".join(str(author) for author in authors) or "none listed"
            else:
                authors_text = "none listed"
            lines.append(
                f"- {hit.title}\n"
                f"  url: {hit.url}\n"
                f"  journal: {hit.extras.get('journal') or 'none listed'}\n"
                f"  year: {hit.extras.get('pub_year') or 'none listed'}\n"
                f"  doi: {hit.extras.get('doi') or 'none listed'}\n"
                f"  authors: {authors_text}\n"
                f"  snippet: {snippet}"
            )
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_sos(query: str) -> str:
    """Smoke wrapper: California Secretary of State business registry.

    Per AC: ``research _smoke-tool sos "SBI Builders"`` should return the
    LLC entry with its registered agent. Lists the top-5 entity hits with
    their entity number / type / status / formed date / agent — bizfileonline
    renders the registered agent directly in the search row, so the AC is
    satisfied from search() alone. Also attempts ``fetch()`` on the top hit
    for richer detail; the per-entity panel is now Okta-gated so
    unauthenticated fetch() typically returns a near-empty Source.
    """
    from research_agent.tools import browser, sos

    async def _run() -> str:
        try:
            results = await sos.search(query, state="CA", max_results=5)
            if not results:
                return f"sos search returned no results for {query!r}"
            lines: list[str] = []
            for hit in results:
                number = hit.extras.get("entity_number") or "?"
                entity_type = hit.extras.get("entity_type") or "?"
                status = hit.extras.get("status") or "?"
                formed = hit.extras.get("formed_date") or "?"
                agent = hit.extras.get("registered_agent") or "?"
                snippet = hit.snippet.replace("\n", " ")
                if len(snippet) > 200:
                    snippet = snippet[:200] + "…"
                lines.append(
                    f"- {hit.title} — {number} — {entity_type} — {status} —"
                    f" formed {formed} — agent {agent}\n"
                    f"  {hit.url}\n"
                    f"  {snippet}"
                )

            top = results[0]
            source = await sos.fetch(top.url)
            if source is None:
                lines.append(
                    f"\nfetch({top.url}) returned None — profile is auth-gated"
                    " (Okta); registered agent shown above is from the search"
                    " row."
                )
            else:
                agent = source.metadata.get("registered_agent") or "?"
                principal = source.metadata.get("principal_address") or "?"
                lines.append(f"\nProfile for {top.title}")
                lines.append(f"  registered_agent: {agent}")
                lines.append(f"  principal_address: {principal}")
            return "\n".join(lines)
        finally:
            # Close the shared Playwright context inside the same event loop
            # we used to open it. The atexit hook tries to shut down on a
            # *new* loop, which deadlocks against the still-pending Chromium
            # IPC and hangs the smoke run after stdout has already flushed.
            try:
                await browser.shutdown()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass

    return asyncio.run(_run())


def _smoke_licensing(query: str) -> str:
    """Smoke wrapper: California State License Board (CSLB) lookup.

    Per AC: ``research _smoke-tool licensing "<license# or contractor>"``
    should return the v1 record (license number, status, expiration). Lists
    the top-5 hits with license number / status / classification / expiration
    / URL, then attempts ``fetch()`` on the top hit and prints a Disciplinary
    History excerpt — the primary due-diligence signal.

    On a zero-result run, the message distinguishes 'CSLB returned 0 hits'
    from 'parser missed every row' so an operator can tell apart a real
    no-such-contractor result from selector drift without re-running.
    """
    from research_agent.tools import browser, licensing

    async def _run() -> str:
        try:
            results, status = await licensing.search(
                query, state="CA", max_results=5, return_diagnostic=True
            )
            if not results:
                if status == "no-hits":
                    return (
                        f"CSLB returned 0 hits for {query!r} "
                        f"(board's results table rendered empty)"
                    )
                if status == "submit-failed":
                    return (
                        f"CSLB search submit failed for {query!r} — "
                        f"diagnostic dump under data/diagnostics/cslb/"
                    )
                if status == "parser-miss":
                    return (
                        f"CSLB result page parser found 0 rows for {query!r}"
                        f" — selector drift suspected; diagnostic dump under "
                        f"data/diagnostics/cslb/"
                    )
                # page-error / unexpected — surface the status code.
                return (
                    f"CSLB licensing search aborted for {query!r} "
                    f"(status={status}); see data/diagnostics/cslb/"
                )
            lines: list[str] = []
            for hit in results:
                number = hit.extras.get("license_number") or "?"
                hit_status = hit.extras.get("status") or "?"
                classification = hit.extras.get("classification") or "?"
                expiration = hit.extras.get("expiration") or "?"
                snippet = hit.snippet.replace("\n", " ")
                if len(snippet) > 200:
                    snippet = snippet[:200] + "…"
                lines.append(
                    f"- {hit.title} — {number} — {classification} — {hit_status} —"
                    f" exp {expiration}\n"
                    f"  {hit.url}\n"
                    f"  {snippet}"
                )

            top = results[0]
            source = await licensing.fetch(top.url)
            if source is None:
                lines.append(
                    f"\nfetch({top.url}) returned None — profile unavailable"
                )
            else:
                disciplinary = source.metadata.get("disciplinary_history") or "—"
                excerpt = disciplinary.replace("\n", " ")
                if len(excerpt) > 400:
                    excerpt = excerpt[:400] + "…"
                lines.append(f"\nProfile for {top.title}")
                lines.append(
                    f"  license_number: {source.metadata.get('license_number') or '?'}"
                )
                lines.append(
                    f"  status: {source.metadata.get('status') or '?'}"
                )
                lines.append(
                    f"  classification: {source.metadata.get('classification') or '?'}"
                )
                lines.append(
                    f"  expiration: {source.metadata.get('expiration') or '?'}"
                )
                lines.append(f"  disciplinary_history: {excerpt}")
            return "\n".join(lines)
        finally:
            try:
                await browser.shutdown()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass

    return asyncio.run(_run())


def _smoke_bbb(query: str) -> str:
    """Smoke wrapper: BBB national search + top-hit profile rollup.

    Per AC: ``research _smoke-tool bbb "SBI Builders"`` should return BBB
    rating + complaint count. Lists the top-5 search hits with rating and
    location, then attempts ``fetch()`` on the top hit and prints rating,
    accreditation, and complaint counts (12mo / 3yr) so an operator can
    eyeball whether the BBB profile DOM is still healthy.
    """
    from research_agent.tools import bbb, browser

    async def _run() -> str:
        try:
            results = await bbb.search(query, max_results=5)
            if not results:
                return f"bbb search returned no results for {query!r}"
            lines: list[str] = []
            for hit in results:
                rating = hit.extras.get("rating") or "?"
                location = hit.extras.get("location") or "?"
                lines.append(
                    f"- {hit.title} — {rating} — {location}\n  {hit.url}"
                )

            top = results[0]
            source = await bbb.fetch(top.url)
            if source is None:
                lines.append(
                    f"\nfetch({top.url}) returned None — profile unavailable"
                )
            else:
                lines.append(f"\nProfile for {top.title}")
                lines.append(f"  rating: {source.metadata.get('rating') or '?'}")
                lines.append(
                    f"  accreditation: {source.metadata.get('accreditation') or '?'}"
                )
                lines.append(
                    f"  complaints_12mo: {source.metadata.get('complaints_12mo') or '?'}"
                )
                lines.append(
                    f"  complaints_3yr: {source.metadata.get('complaints_3yr') or '?'}"
                )
            return "\n".join(lines)
        finally:
            try:
                await browser.shutdown()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass

    return asyncio.run(_run())


def _smoke_calaccess(query: str) -> str:
    """Smoke wrapper: California Cal-Access / Power Search campaign finance.

    Per AC: ``research _smoke-tool calaccess "Gavin Newsom"`` should return
    recent donations. Lists the top-5 contribution hits with donor /
    committee / amount / date / permalink, then attempts ``fetch()`` on the
    top hit and prints the rolled-up record so an operator can eyeball
    whether the Power Search SPA is still healthy.
    """
    from research_agent.tools import browser, calaccess

    async def _run() -> str:
        try:
            results = await calaccess.search(
                query, kind="contributions", max_results=5
            )
            if not results:
                return f"calaccess search returned no results for {query!r}"
            lines: list[str] = []
            for hit in results:
                donor = hit.extras.get("donor") or "?"
                committee = hit.extras.get("committee") or "?"
                amount = hit.extras.get("amount") or "?"
                date = hit.extras.get("date") or "?"
                lines.append(
                    f"- {donor} → {committee} — {amount} — {date}\n  {hit.url}"
                )

            top = results[0]
            source = await calaccess.fetch(top.url)
            if source is None:
                lines.append(
                    f"\nfetch({top.url}) returned None — detail page unavailable"
                )
            else:
                lines.append(f"\nRollup for {top.title}")
                lines.append(f"  amount: {source.metadata.get('amount') or '?'}")
                lines.append(f"  date: {source.metadata.get('date') or '?'}")
                lines.append(f"  parties: {source.metadata.get('parties') or '?'}")
                lines.append(
                    f"  filing_reference: {source.metadata.get('filing_reference') or '?'}"
                )
            return "\n".join(lines)
        finally:
            try:
                await browser.shutdown()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass

    return asyncio.run(_run())


def _smoke_sanctions(query: str) -> str:
    """Smoke wrapper: OFAC SDN / EU / UK sanctions lookup.

    Per AC: ``research _smoke-tool sanctions "Yevgeny Prigozhin"`` should
    surface the SDN designation. Each line shows the list, programs,
    designation date, agency, sample aliases, and the sanctionssearch
    permalink so an operator can eyeball whether the bulk index is healthy.
    """
    from research_agent.tools import sanctions

    async def _run() -> str:
        results = await sanctions.search(query, max_results=5)
        if not results:
            return f"sanctions search returned no results for {query!r}"
        lines: list[str] = []
        for hit in results:
            list_kind = hit.extras.get("list_kind") or "?"
            programs = ", ".join(hit.extras.get("programs") or []) or "?"
            designation = hit.extras.get("designation_date") or "?"
            agency = hit.extras.get("sanctioning_agency") or "?"
            aliases = hit.extras.get("aliases") or []
            alias_sample = ", ".join(a.get("name", "") for a in aliases[:3]) or "—"
            fuzzy = " [fuzzy]" if hit.extras.get("fuzzy") else ""
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(
                f"- {hit.title} — {list_kind} — {programs} — {designation} —"
                f" {agency}{fuzzy}\n"
                f"  aliases: {alias_sample}\n"
                f"  {hit.url}\n"
                f"  {snippet}"
            )
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_usaspending(query: str) -> str:
    """Smoke wrapper: USAspending awards search returning the top-5 hits.

    Per AC: ``research _smoke-tool usaspending "Booz Allen Hamilton"`` should
    surface recent federal contracts with recipient, award amount, type,
    awarding agency, action date, and permalink so an operator can eyeball
    whether the public POST search endpoint is reachable.
    """
    from research_agent.tools import usaspending

    async def _run() -> str:
        results = await usaspending.search(query, max_results=5)
        if not results:
            return f"usaspending search returned no results for {query!r}"
        lines: list[str] = []
        for hit in results:
            recipient = hit.extras.get("recipient_name") or "?"
            amount = hit.extras.get("award_amount")
            award_type = hit.extras.get("award_type") or "?"
            agency = hit.extras.get("awarding_agency") or "?"
            action_date = hit.extras.get("action_date") or "?"
            no_bid = hit.extras.get("no_bid_flag")
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            no_bid_label = " [no-bid]" if no_bid else ""
            lines.append(
                f"- {recipient} — ${amount} — {award_type} — {agency} —"
                f" {action_date}{no_bid_label}\n"
                f"  {hit.url}\n"
                f"  {snippet}"
            )
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_gdelt(query: str) -> str:
    """Smoke wrapper: GDELT 2.0 DOC search + tone timeline.

    Per AC: ``research _smoke-tool gdelt "Anysphere Cursor"`` should surface
    recent global news / broadcast TV mentions plus a sentiment-over-time
    series. Lists the top-5 article hits, then prints a one-line summary of
    the ``tone_timeline`` series (point count + first/last datapoints) so
    an operator can eyeball both surfaces in one shot.
    """
    from research_agent.tools import gdelt

    async def _run() -> str:
        results = await gdelt.search(query, max_results=5)
        lines: list[str] = []
        if not results:
            lines.append(f"gdelt search returned no results for {query!r}")
        else:
            for hit in results:
                domain = hit.extras.get("domain") or "?"
                language = hit.extras.get("language") or "?"
                seendate = hit.extras.get("seendate") or "?"
                lines.append(
                    f"- {hit.title}\n"
                    f"  {hit.url}\n"
                    f"  {domain} | {language} | {seendate}"
                )

        tone = await gdelt.tone_timeline(query)
        lines.append(f"\ntone_points: {len(tone)}")
        if tone:
            first = tone[0]
            last = tone[-1]
            lines.append(
                f"  first: {first['datetime'].isoformat()} value={first['value']}"
            )
            lines.append(
                f"  last:  {last['datetime'].isoformat()} value={last['value']}"
            )
        return "\n".join(lines)

    return asyncio.run(_run())


def _smoke_congress(query: str) -> str:
    """Smoke wrapper: Congress.gov v3 bill search returning the top-5 hits.

    Per AC: ``research _smoke-tool congress "Inflation Reduction Act"`` should
    surface bill rows with title, congress + session, sponsor, latest action,
    and permalink so an operator can eyeball whether the v3 API is reachable
    and the api.data.gov key is healthy.
    """
    from research_agent.tools import congress

    async def _run() -> str:
        results = await congress.search(query, kind="bill", max_results=5)
        if not results:
            return f"congress search returned no results for {query!r}"
        lines: list[str] = []
        for hit in results:
            congress_no = hit.extras.get("congress") or "?"
            session = hit.extras.get("session") or "?"
            bill_type = (hit.extras.get("bill_type") or "?").upper()
            number = hit.extras.get("bill_number") or "?"
            sponsor = hit.extras.get("sponsor") or "?"
            latest = hit.extras.get("latest_action") or "?"
            latest_date = hit.extras.get("latest_action_date") or "?"
            snippet = hit.snippet.replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(
                f"- {hit.title}\n"
                f"  {bill_type} {number} — {congress_no}th Congress (Sess. {session}) —"
                f" Sponsor: {sponsor}\n"
                f"  Latest: {latest_date} — {latest}\n"
                f"  {hit.url}\n"
                f"  {snippet}"
            )
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


def _smoke_archive_today(url: str) -> str:
    """Smoke wrapper: submit ``url`` to archive.today and return the archive URL.

    Per AC: ``research _smoke-tool archive_today https://example.com`` should
    return the resulting ``archive.today/<id>`` URL — or a clear "no URL"
    message if archive.today served a captcha or otherwise refused.
    """
    from research_agent.tools import archive

    archive_url = asyncio.run(archive.archive_today_save(url))
    if archive_url is None:
        return f"archive.today save returned no URL for {url}"
    return archive_url


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
    "archive_today": _smoke_archive_today,
    "local_corpus": _smoke_local_corpus,
    "arxiv": _smoke_arxiv,
    "audio": _smoke_audio,
    "bbb": _smoke_bbb,
    "calaccess": _smoke_calaccess,
    "commons_search": _smoke_commons_search,
    "edgar": _smoke_edgar,
    "courtlistener": _smoke_courtlistener,
    "scholar": _smoke_scholar,
    "linkedin": _smoke_linkedin,
    "loc_search": _smoke_loc,
    "fedregister": _smoke_fedregister,
    "gallica_search": _smoke_gallica_search,
    "iarchive_search": _smoke_iarchive,
    "nonprofits": _smoke_nonprofits,
    "fec": _smoke_fec,
    "congress": _smoke_congress,
    "lda": _smoke_lda,
    "littlesis": _smoke_littlesis,
    "opencorporates": _smoke_opencorporates,
    "trove_search": _smoke_trove_search,
    "wikidata_search": _smoke_wikidata_search,
    "wikisource_search": _smoke_wikisource_search,
    "openlibrary_search": _smoke_openlibrary_search,
    "persee_search": _smoke_persee_search,
    "sos": _smoke_sos,
    "licensing": _smoke_licensing,
    "sanctions": _smoke_sanctions,
    "usaspending": _smoke_usaspending,
    "gdelt": _smoke_gdelt,
    "news": _smoke_news,
    "reddit": _smoke_reddit,
    "pdf": _smoke_pdf,
    "ocr": _smoke_ocr,
    "youtube": _smoke_youtube,
}

__all__ = [
    "BaseSearchPayload",
    "KindEntry",
    "SearchResult",
    "Source",
    "SourceKind",
    "TOOL_REGISTRY",
    "iter_kinds",
    "register_kind",
]
