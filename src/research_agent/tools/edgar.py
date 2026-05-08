"""SEC EDGAR connector (issue #98).

Public surface:

* ``async def search(query, *, form_type=None, max_results=20) -> list[SearchResult]``
  hits EDGAR's full-text search index. ``form_type`` accepts a single form
  string (``"8-K"``) or a list (``["10-K", "10-Q"]``) joined by ``,`` per the
  endpoint contract.
* ``async def fetch(url, timeout=30.0) -> Source | None`` opens a filing
  index page and returns the rolled-up text of the primary doc — HTML body
  for 10-K/Q/8-K, structured-XML summary for Form 4 insider trades.

SEC enforces a contact email in the User-Agent and a 10 req/sec cap. We
require ``RESEARCH_USER_AGENT`` to contain an ``@`` (failing loudly here
beats silently getting 403'd) and gate every call to ≈9 req/s.

Filings are immutable post-submission, so primary docs are cached at
``corpus/.cache/edgar/<accession>.{html,xml}``; a second ``fetch`` of the
same index URL skips the network for the doc body entirely.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura

from research_agent import config
from research_agent.tools._errors import MissingCredentialError
from research_agent.tools.models import SearchResult, Source

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
_FILING_BASE = "https://www.sec.gov"
# 10 req/sec is the SEC ceiling; 0.11 keeps us comfortably under at ≈9/s.
_RATE_LIMIT_INTERVAL = 0.11
_CACHE_DIR = Path("corpus/.cache/edgar")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_DOC_LINK_RE = re.compile(
    r'href="(?P<href>/Archives/edgar/data/[^"]+\.(?:htm|html|xml|txt))"',
    re.IGNORECASE,
)
_FORM_HEADER_RE = re.compile(
    r"Form\s+(?:Type\s*:?\s*)?(?:<[^>]+>\s*)?([0-9A-Z][0-9A-Z\-/]*)",
    re.IGNORECASE,
)
_ACCESSION_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")

_rate_lock = asyncio.Lock()
_last_call_monotonic: float | None = None


# ---------------------------------------------------------------------------
# Config / rate-limit helpers
# ---------------------------------------------------------------------------


def _resolve_user_agent() -> str:
    """Return the configured UA. Raise RuntimeError if no contact email is set.

    SEC requires a contact email in the User-Agent header. The default UA
    declared in :data:`config.EXPECTED_ENV_KEYS` reads ``contact unset`` —
    that placeholder will get a 403 from EDGAR, so we'd rather fail here
    with an actionable message than silently rack up blocked requests.
    """
    ua = config.get("RESEARCH_USER_AGENT") or ""
    if "@" not in ua:
        raise MissingCredentialError(
            "SEC EDGAR requires a contact email in the User-Agent. "
            "Set RESEARCH_USER_AGENT in your .env to e.g. "
            '"research-agent your-name@example.com".'
        )
    return ua


async def _rate_limit_gate() -> None:
    """Block until at least ``_RATE_LIMIT_INTERVAL`` has passed since the last call."""
    global _last_call_monotonic
    async with _rate_lock:
        if _last_call_monotonic is not None:
            elapsed = time.monotonic() - _last_call_monotonic
            wait = _RATE_LIMIT_INTERVAL - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
        _last_call_monotonic = time.monotonic()


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


def _parse_file_date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _coerce_form_param(form_type: str | list[str] | tuple[str, ...] | None) -> str | None:
    if form_type is None:
        return None
    if isinstance(form_type, str):
        return form_type
    return ",".join(str(f) for f in form_type if f)


def _primary_company(display_names: list[str]) -> str:
    """Pick the issuer from EDGAR's ``display_names`` list.

    Form 4 hits list both the reporting owner and the issuer; the issuer is
    typically the entry whose name omits a trailing CIK (or, failing that,
    the longest entry — heuristic but stable enough for a snippet/title).
    """
    if not display_names:
        return ""
    cleaned = [d.strip() for d in display_names if d and d.strip()]
    if not cleaned:
        return ""
    return cleaned[0]


def _build_permalink(cik: str, accession: str) -> str:
    """Return the canonical filing-index URL for ``accession`` under ``cik``."""
    cik_int = str(int(cik)) if cik and cik.lstrip("0") else cik
    accession_no_dashes = accession.replace("-", "")
    return (
        f"{_FILING_BASE}/Archives/edgar/data/{cik_int}/"
        f"{accession_no_dashes}/{accession}-index.htm"
    )


def _highlight_snippet(hit: dict[str, Any], fallback: str) -> str:
    highlight = hit.get("highlight") or {}
    if isinstance(highlight, dict):
        for value in highlight.values():
            if isinstance(value, list) and value:
                return _strip_html(str(value[0]))
            if isinstance(value, str) and value:
                return _strip_html(value)
    return fallback


def _build_search_result(hit: dict[str, Any]) -> SearchResult | None:
    source = hit.get("_source") or {}
    accession = (source.get("adsh") or "").strip()
    ciks = source.get("ciks") or []
    cik = ciks[0] if ciks else ""
    if not accession or not cik:
        return None

    display_names = source.get("display_names") or []
    company = _primary_company(display_names)
    form = (source.get("form") or "").strip()
    file_type = (source.get("file_type") or "").strip()
    file_date = _parse_file_date(source.get("file_date"))

    permalink = _build_permalink(cik, accession)
    snippet = _highlight_snippet(hit, company or accession)
    title = f"{form} — {company}".strip(" —") if form or company else accession

    return SearchResult(
        url=permalink,
        title=title,
        snippet=snippet,
        published_at=file_date,
        source_kind="sec",
        extras={
            "cik": cik,
            "accession": accession,
            "form": form,
            "company": company,
            "file_type": file_type,
        },
    )


async def search(
    query: str,
    *,
    form_type: str | list[str] | tuple[str, ...] | None = None,
    max_results: int = 20,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Hit EDGAR full-text search and return up to ``max_results`` hits.

    ``form_type`` filters by form (``"8-K"`` or ``["10-K", "10-Q"]``).
    Returns ``[]`` on any HTTP error, non-200, or unparseable response —
    callers compose multiple connectors and a single failure should never
    crash the planner.
    """
    params: dict[str, str] = {"q": query}
    forms = _coerce_form_param(form_type)
    if forms:
        params["forms"] = forms

    headers = {"User-Agent": _resolve_user_agent(), "Accept": "application/json"}

    await _rate_limit_gate()

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
        ) as client:
            response = await client.get(_SEARCH_URL, params=params)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("edgar search failed for %r: %s", query, exc)
        return []

    if response.status_code != 200:
        logger.warning(
            "edgar search returned HTTP %s for %r", response.status_code, query
        )
        return []

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("edgar search returned non-JSON for %r: %s", query, exc)
        return []

    hits = (((payload.get("hits") or {}).get("hits")) or [])
    out: list[SearchResult] = []
    for hit in hits[:max_results]:
        result = _build_search_result(hit)
        if result is not None:
            out.append(result)
    return out


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


def _strip_html(html: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html)).strip()


def _detect_form(html: str) -> str | None:
    match = _FORM_HEADER_RE.search(html)
    if match:
        return match.group(1).strip()
    return None


def _detect_accession(url: str, html: str) -> str | None:
    for source_text in (url, html):
        match = _ACCESSION_RE.search(source_text)
        if match:
            return match.group(1)
    return None


def _detect_cik_from_url(url: str) -> str | None:
    match = re.search(r"/Archives/edgar/data/(\d+)/", url)
    if match:
        return match.group(1)
    return None


def _parse_filing_index(
    html: str, base_url: str
) -> tuple[str | None, str | None]:
    """Return ``(primary_doc_url, form)`` parsed from a filing-index page.

    Strategy:
      1. Detect the filing's form from the page header.
      2. Walk the document table — pick the first row whose Type column
         matches the detected form. For Form 4 prefer the row's ``.xml``
         link (structured ownership data); for everything else prefer
         ``.htm`` so we stay HTML-extractor-friendly.
      3. Fallback: first .xml for Form 4, first .htm otherwise, then any.
    """
    form = _detect_form(html)
    candidates: list[tuple[str, str]] = []  # (row_form, href)
    for row_match in _TR_RE.finditer(html):
        row = row_match.group(1)
        link_match = _DOC_LINK_RE.search(row)
        if not link_match:
            continue
        href = link_match.group("href")
        if "-index" in href.lower():
            continue
        cells = [_strip_html(c) for c in _TD_RE.findall(row)]
        row_form = cells[3] if len(cells) >= 4 else ""
        candidates.append((row_form, href))

    if not candidates:
        return None, form

    is_form4 = form is not None and form.upper().rstrip("/A").rstrip() == "4"

    def _absolutise(href: str) -> str:
        return urljoin(base_url, href)

    if form:
        for row_form, href in candidates:
            if row_form.upper() == form.upper():
                if is_form4 and not href.lower().endswith(".xml"):
                    # Look for an xml sibling row in the same filing.
                    for r2_form, r2_href in candidates:
                        if r2_form.upper() == form.upper() and r2_href.lower().endswith(
                            ".xml"
                        ):
                            return _absolutise(r2_href), form
                return _absolutise(href), form

    if is_form4:
        for _, href in candidates:
            if href.lower().endswith(".xml") and "filingsummary" not in href.lower():
                return _absolutise(href), form

    for _, href in candidates:
        if href.lower().endswith((".htm", ".html")):
            return _absolutise(href), form

    return _absolutise(candidates[0][1]), form


def _summarize_form4(xml_text: str) -> str:
    """Flatten an ``ownershipDocument`` Form 4 XML into a readable summary.

    Returns ``""`` if the XML can't be parsed or isn't an ownership doc;
    callers fall back to the raw text in that case so we never silently
    drop a filing.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return ""
    if root.tag != "ownershipDocument":
        return ""

    lines: list[str] = []

    issuer = root.find("issuer")
    if issuer is not None:
        name = (issuer.findtext("issuerName") or "").strip()
        ticker = (issuer.findtext("issuerTradingSymbol") or "").strip()
        if name:
            label = f"Issuer: {name}"
            if ticker:
                label += f" ({ticker})"
            lines.append(label)

    for owner in root.findall("reportingOwner"):
        owner_name = (owner.findtext("reportingOwnerId/rptOwnerName") or "").strip()
        rel = owner.find("reportingOwnerRelationship")
        rel_parts: list[str] = []
        if rel is not None:
            for tag, label in (
                ("isDirector", "Director"),
                ("isOfficer", "Officer"),
                ("isTenPercentOwner", "10%Owner"),
                ("isOther", "Other"),
            ):
                value = (rel.findtext(tag) or "").strip().lower()
                if value in {"1", "true"}:
                    rel_parts.append(label)
            title = (rel.findtext("officerTitle") or "").strip()
            if title:
                rel_parts.append(f"Title={title}")
        line = f"Reporting Owner: {owner_name}" if owner_name else "Reporting Owner"
        if rel_parts:
            line += f" [{', '.join(rel_parts)}]"
        lines.append(line)

    nd_txns = root.findall("nonDerivativeTable/nonDerivativeTransaction")
    if nd_txns:
        lines.append("")
        lines.append("Non-derivative transactions:")
        for tx in nd_txns:
            lines.append(_format_form4_txn(tx))

    deriv_txns = root.findall("derivativeTable/derivativeTransaction")
    if deriv_txns:
        lines.append("")
        lines.append("Derivative transactions:")
        for tx in deriv_txns:
            lines.append(_format_form4_txn(tx))

    return "\n".join(lines).strip()


def _format_form4_txn(tx: ET.Element) -> str:
    sec = (tx.findtext("securityTitle/value") or "").strip()
    txn_date = (tx.findtext("transactionDate/value") or "").strip()
    code = (tx.findtext("transactionCoding/transactionCode") or "").strip()
    shares = (tx.findtext("transactionAmounts/transactionShares/value") or "").strip()
    price = (tx.findtext("transactionAmounts/transactionPricePerShare/value") or "").strip()
    ad = (
        tx.findtext("transactionAmounts/transactionAcquiredDisposedCode/value") or ""
    ).strip()
    post = (
        tx.findtext("postTransactionAmounts/sharesOwnedFollowingTransaction/value")
        or ""
    ).strip()
    return (
        f"  {txn_date} | code={code} | A/D={ad} | shares={shares} "
        f"| price={price} | post={post} | {sec}".rstrip()
    )


def _extract_html_text(html: str) -> str:
    if not html:
        return ""
    try:
        extracted = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
        )
    except Exception:  # noqa: BLE001 — never crash on extractor errors
        extracted = None
    if extracted and extracted.strip():
        return extracted.strip()
    # Fallback: best-effort tag strip so we always return *something* for
    # filings that defeat trafilatura (e.g. pre-XBRL boilerplate-heavy 10-K).
    return _strip_html(html)


def _cache_path(accession: str, suffix: str) -> Path:
    safe = accession.replace("/", "_")
    return _CACHE_DIR / f"{safe}{suffix}"


def _write_cache(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(body)
    tmp.replace(path)


async def _http_get(
    url: str, timeout: float, *, accept: str = "*/*"
) -> tuple[int | None, bytes | None, str]:
    headers = {
        "User-Agent": _resolve_user_agent(),
        "Accept": accept,
    }
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
        ) as client:
            response = await client.get(url)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("edgar fetch failed for %s: %s", url, exc)
        return None, None, ""
    return response.status_code, response.content, response.text


async def fetch(url: str, timeout: float = 30.0) -> Source | None:
    """Open a filing index URL and return a :class:`Source` for the primary doc.

    For Form 4, the primary doc is the ``ownershipDocument`` XML — we
    flatten its issuer/owner/transaction fields into ``cleaned_text``.
    For 10-K/10-Q/8-K (and the long tail), we feed the primary ``.htm`` body
    through trafilatura. Returns ``None`` on transport/HTTP/parse failure.
    """
    if not url:
        return None

    parsed = urlparse(url)
    if not parsed.netloc:
        return None

    await _rate_limit_gate()
    status, _content, index_text = await _http_get(url, timeout, accept="text/html")
    if status is None or status >= 400 or not index_text:
        if status is not None and status >= 400:
            logger.warning("edgar fetch returned HTTP %s for %s", status, url)
        return None

    primary_url, form = _parse_filing_index(index_text, url)
    if not primary_url:
        logger.warning("edgar fetch could not resolve primary doc for %s", url)
        return None

    accession = _detect_accession(url, index_text)
    if not accession:
        accession = primary_url.rsplit("/", 1)[-1].split(".")[0]

    cik = _detect_cik_from_url(url) or _detect_cik_from_url(primary_url) or ""

    is_xml = primary_url.lower().endswith(".xml")
    suffix = ".xml" if is_xml else ".html"
    cache_path = _cache_path(accession, suffix)

    if cache_path.exists():
        body = cache_path.read_bytes()
    else:
        await _rate_limit_gate()
        primary_status, primary_bytes, _primary_text = await _http_get(
            primary_url, timeout, accept="application/xml" if is_xml else "text/html"
        )
        if (
            primary_status is None
            or primary_status >= 400
            or primary_bytes is None
        ):
            if primary_status is not None and primary_status >= 400:
                logger.warning(
                    "edgar fetch primary doc HTTP %s for %s", primary_status, primary_url
                )
            return None
        body = primary_bytes
        _write_cache(cache_path, body)

    body_text = body.decode("utf-8", errors="replace")

    if is_xml:
        cleaned_text = _summarize_form4(body_text) or _strip_html(body_text)
    else:
        cleaned_text = _extract_html_text(body_text)

    if not cleaned_text:
        return None

    company = ""
    company_match = re.search(
        r"<span[^>]*class=\"companyName\"[^>]*>([^<]+)", index_text, re.IGNORECASE
    )
    if company_match:
        company = _strip_html(company_match.group(1)).strip()

    title_bits = [b for b in (form, company) if b]
    title = " — ".join(title_bits) if title_bits else (accession or url)

    file_date_match = re.search(
        r"Filing Date[^0-9]{0,40}(\d{4}-\d{2}-\d{2})", index_text, re.IGNORECASE
    )
    file_date = file_date_match.group(1) if file_date_match else None

    metadata: dict[str, Any] = {
        "accession": accession,
        "cik": cik,
        "form": form,
        "primary_doc_url": primary_url,
        "file_date": file_date,
    }

    return Source(
        url=url,
        title=title,
        cleaned_text=cleaned_text,
        raw_html=index_text if not is_xml else None,
        fetched_at=datetime.now(UTC),
        source_kind="sec",
        metadata=metadata,
    )


def reset_for_tests() -> None:
    """Clear per-process rate-limit state. Test-only."""
    global _last_call_monotonic, _rate_lock
    _last_call_monotonic = None
    _rate_lock = asyncio.Lock()


__all__ = ["fetch", "reset_for_tests", "search"]
