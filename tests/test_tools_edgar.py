"""Tests for `research_agent.tools.edgar` (issue #98)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from research_agent.tools import edgar

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_user_agent(monkeypatch):
    monkeypatch.setenv("RESEARCH_USER_AGENT", "research-agent test@example.com")
    yield


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    edgar.reset_for_tests()
    monkeypatch.setattr(edgar.asyncio, "sleep", AsyncMock())
    yield
    edgar.reset_for_tests()


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "edgar-cache"
    monkeypatch.setattr(edgar, "_CACHE_DIR", target)
    return target


# A FTS hit shape that mirrors what efts.sec.gov actually returns.
_SEARCH_PAYLOAD = {
    "hits": {
        "total": {"value": 2},
        "hits": [
            {
                "_id": "0001140361-23-040296:dlhe_form4-040301.xml",
                "_source": {
                    "ciks": ["0000858877"],
                    "period_of_report": "2023-08-08",
                    "root_form": "8-K",
                    "file_date": "2023-08-09",
                    "form": "8-K",
                    "adsh": "0000858877-23-000018",
                    "display_names": ["Cisco Systems, Inc. (CIK 0000858877)"],
                    "file_type": "8-K",
                    "items": ["1.05"],
                },
                "highlight": {
                    "content": [
                        "<em>cybersecurity</em> incident disclosure under Item 1.05"
                    ]
                },
            },
            {
                "_id": "0000858877-23-000019:cisco-8k.htm",
                "_source": {
                    "ciks": ["0000858877"],
                    "file_date": "2023-09-01",
                    "form": "8-K",
                    "adsh": "0000858877-23-000019",
                    "display_names": ["Cisco Systems, Inc. (CIK 0000858877)"],
                    "file_type": "8-K",
                    "items": [],
                },
            },
        ],
    }
}


_INDEX_HTML_10K = """\
<html><head><title>EDGAR Filing</title></head><body>
<div class="formGrouping">
  <div class="info">Form 10-K - Annual Report</div>
  <span class="companyName">Cisco Systems, Inc.</span>
  <div class="info">Filing Date 2024-09-07</div>
</div>
<table class="tableFile" summary="Document Format Files">
  <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
  <tr>
    <td>1</td>
    <td>10-K</td>
    <td><a href="/Archives/edgar/data/858877/000085887724000007/csco-20240727.htm">csco.htm</a></td>
    <td>10-K</td>
    <td>123456</td>
  </tr>
  <tr>
    <td>2</td>
    <td>EX-21</td>
    <td><a href="/Archives/edgar/data/858877/000085887724000007/exh21.htm">exh21.htm</a></td>
    <td>EX-21</td>
    <td>2345</td>
  </tr>
</table>
</body></html>
"""

_PRIMARY_HTML_10K = """\
<html><body>
<h1>Cisco Systems, Inc. — Annual Report (10-K)</h1>
<p>Cisco designs, manufactures, and sells networking and security products
worldwide. Total revenue for fiscal 2024 was $53.8 billion. The company faces
risks from cybersecurity incidents, geopolitical tension, and macro
uncertainty. Management discusses ongoing investments in artificial
intelligence platforms and observability software across the enterprise
portfolio. The Company entered into a definitive agreement to acquire
Splunk Inc. on September 21, 2023 for approximately $28 billion in cash.
The acquisition closed on March 18, 2024.</p>
</body></html>
"""

_INDEX_HTML_FORM4 = """\
<html><head><title>EDGAR Filing</title></head><body>
<div class="formGrouping">
  <div class="info">Form 4 - Statement of Changes in Beneficial Ownership</div>
  <span class="companyName">Cisco Systems, Inc.</span>
  <div class="info">Filing Date 2024-08-15</div>
</div>
<table class="tableFile">
  <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
  <tr>
    <td>1</td>
    <td>Primary Document</td>
    <td><a href="/Archives/edgar/data/858877/000114036124034444/csco_form4.xml">form4.xml</a></td>
    <td>4</td>
    <td>3456</td>
  </tr>
</table>
</body></html>
"""

_FORM4_XML = """\
<?xml version="1.0"?>
<ownershipDocument>
  <issuer>
    <issuerCik>0000858877</issuerCik>
    <issuerName>CISCO SYSTEMS, INC.</issuerName>
    <issuerTradingSymbol>CSCO</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001234567</rptOwnerCik>
      <rptOwnerName>ROBBINS CHARLES H</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <isOfficer>1</isOfficer>
      <officerTitle>Chairman and Chief Executive Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-08-13</value></transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>S</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>48.50</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>123456</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


def _patch_httpx(monkeypatch, *, responder):
    """Replace ``httpx.AsyncClient`` with a fake driven by ``responder(url)``.

    ``responder`` is called with the full URL (including query string) and
    returns ``(status_code, body_bytes, body_text)``.
    """
    captured: dict[str, list] = {"urls": [], "headers": [], "params": []}

    class _FakeResp:
        def __init__(self, status: int, body: bytes, text: str) -> None:
            self.status_code = status
            self.content = body
            self.text = text

        def json(self):
            return json.loads(self.text)

    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        captured["headers"].append(kwargs.get("headers"))

        class _Client:
            async def get(self, url, *, params=None, **_kwargs):
                captured["urls"].append(url)
                captured["params"].append(params)
                full_url = url
                if params:
                    qs = "&".join(f"{k}={v}" for k, v in params.items())
                    full_url = f"{url}?{qs}"
                status, body, text = responder(full_url)
                return _FakeResp(status, body, text)

        yield _Client()

    monkeypatch.setattr(edgar.httpx, "AsyncClient", _client_factory)
    return captured


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


async def test_search_returns_search_results_with_sec_kind(monkeypatch):
    payload = json.dumps(_SEARCH_PAYLOAD).encode()

    def _respond(url: str):
        return 200, payload, payload.decode()

    captured = _patch_httpx(monkeypatch, responder=_respond)

    results = await edgar.search("Cisco cybersecurity")

    assert len(results) == 2
    assert all(r.source_kind == "sec" for r in results)
    first = results[0]
    assert first.extras["accession"] == "0000858877-23-000018"
    assert first.extras["cik"] == "0000858877"
    assert first.extras["form"] == "8-K"
    assert first.extras["company"].startswith("Cisco Systems")
    assert first.extras["file_type"] == "8-K"
    # Permalink resolves to the index.htm under the no-dash accession folder.
    assert first.url == (
        "https://www.sec.gov/Archives/edgar/data/858877/"
        "000085887723000018/0000858877-23-000018-index.htm"
    )
    assert "8-K" in first.title
    assert first.published_at is not None
    assert first.published_at.year == 2023
    assert first.published_at.month == 8
    # Highlight comes through stripped of <em> tags.
    assert "cybersecurity" in first.snippet
    assert "<em>" not in first.snippet
    # UA header is plumbed through.
    sent_headers = captured["headers"][0]
    assert "test@example.com" in sent_headers["User-Agent"]


async def test_search_passes_form_type_filter(monkeypatch):
    payload = json.dumps({"hits": {"hits": []}}).encode()

    def _respond(url: str):
        return 200, payload, payload.decode()

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await edgar.search("anything", form_type="8-K")

    params = captured["params"][0]
    assert params["forms"] == "8-K"
    assert params["q"] == "anything"


async def test_search_joins_form_type_list(monkeypatch):
    payload = json.dumps({"hits": {"hits": []}}).encode()

    def _respond(url: str):
        return 200, payload, payload.decode()

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await edgar.search("anything", form_type=["10-K", "10-Q"])

    assert captured["params"][0]["forms"] == "10-K,10-Q"


async def test_search_omits_forms_param_when_none(monkeypatch):
    payload = json.dumps({"hits": {"hits": []}}).encode()

    def _respond(url: str):
        return 200, payload, payload.decode()

    captured = _patch_httpx(monkeypatch, responder=_respond)

    await edgar.search("anything")

    assert "forms" not in (captured["params"][0] or {})


async def test_search_returns_empty_on_http_error(monkeypatch):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(edgar.httpx, "AsyncClient", _client_factory)

    assert await edgar.search("anything") == []


async def test_search_returns_empty_on_non_200(monkeypatch):
    def _respond(url: str):
        return 503, b"", ""

    _patch_httpx(monkeypatch, responder=_respond)

    assert await edgar.search("anything") == []


async def test_search_returns_empty_on_non_json(monkeypatch):
    def _respond(url: str):
        return 200, b"<html>", "<html>"

    _patch_httpx(monkeypatch, responder=_respond)

    assert await edgar.search("anything") == []


async def test_user_agent_missing_email_raises(monkeypatch):
    monkeypatch.setenv("RESEARCH_USER_AGENT", "research-agent/0.1")

    with pytest.raises(RuntimeError, match="contact email"):
        await edgar.search("anything")


async def test_search_rate_limit_gate_is_awaited(monkeypatch):
    """Two concurrent ``search`` calls must both pass through the gate."""
    clock = [100.0]

    def fake_monotonic() -> float:
        return clock[0]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(edgar.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(edgar.asyncio, "sleep", fake_sleep)

    payload = json.dumps({"hits": {"hits": []}}).encode()

    def _respond(url: str):
        return 200, payload, payload.decode()

    _patch_httpx(monkeypatch, responder=_respond)

    await asyncio.gather(edgar.search("a"), edgar.search("b"))

    assert any(s > 0 for s in sleep_calls), (
        f"expected at least one >0 sleep through the rate gate; got {sleep_calls!r}"
    )


# ---------------------------------------------------------------------------
# fetch()
# ---------------------------------------------------------------------------


_INDEX_URL_10K = (
    "https://www.sec.gov/Archives/edgar/data/858877/"
    "000085887724000007/0000858877-24-000007-index.htm"
)
_PRIMARY_URL_10K = (
    "https://www.sec.gov/Archives/edgar/data/858877/"
    "000085887724000007/csco-20240727.htm"
)
_INDEX_URL_FORM4 = (
    "https://www.sec.gov/Archives/edgar/data/858877/"
    "000114036124034444/0001140361-24-034444-index.htm"
)
_PRIMARY_URL_FORM4 = (
    "https://www.sec.gov/Archives/edgar/data/858877/"
    "000114036124034444/csco_form4.xml"
)


async def test_fetch_10k_index_resolves_primary_htm(monkeypatch, cache_dir: Path):
    def _respond(url: str):
        if url == _INDEX_URL_10K:
            body = _INDEX_HTML_10K.encode()
            return 200, body, _INDEX_HTML_10K
        if url == _PRIMARY_URL_10K:
            body = _PRIMARY_HTML_10K.encode()
            return 200, body, _PRIMARY_HTML_10K
        return 404, b"", ""

    captured = _patch_httpx(monkeypatch, responder=_respond)

    source = await edgar.fetch(_INDEX_URL_10K)

    assert source is not None
    assert source.source_kind == "sec"
    assert source.url == _INDEX_URL_10K
    assert "Cisco" in source.cleaned_text
    assert "Splunk" in source.cleaned_text
    assert source.metadata["primary_doc_url"] == _PRIMARY_URL_10K
    assert source.metadata["form"] == "10-K"
    assert source.metadata["cik"] == "858877"
    # Both the index and the primary doc were fetched.
    assert _INDEX_URL_10K in captured["urls"]
    assert _PRIMARY_URL_10K in captured["urls"]
    # The primary HTML was cached on disk.
    cached = list(cache_dir.glob("*.html"))
    assert len(cached) == 1


async def test_fetch_form4_uses_xml_summary(monkeypatch, cache_dir: Path):
    def _respond(url: str):
        if url == _INDEX_URL_FORM4:
            body = _INDEX_HTML_FORM4.encode()
            return 200, body, _INDEX_HTML_FORM4
        if url == _PRIMARY_URL_FORM4:
            body = _FORM4_XML.encode()
            return 200, body, _FORM4_XML
        return 404, b"", ""

    _patch_httpx(monkeypatch, responder=_respond)

    source = await edgar.fetch(_INDEX_URL_FORM4)

    assert source is not None
    assert source.source_kind == "sec"
    assert source.metadata["primary_doc_url"] == _PRIMARY_URL_FORM4
    assert source.metadata["form"] == "4"
    assert "CISCO SYSTEMS" in source.cleaned_text
    assert "ROBBINS CHARLES H" in source.cleaned_text
    # Transaction code S (open-market sale) made it into the summary.
    assert "code=S" in source.cleaned_text
    assert "10000" in source.cleaned_text
    cached = list(cache_dir.glob("*.xml"))
    assert len(cached) == 1


async def test_fetch_caches_primary_doc(monkeypatch, cache_dir: Path):
    def _respond(url: str):
        if url == _INDEX_URL_10K:
            return 200, _INDEX_HTML_10K.encode(), _INDEX_HTML_10K
        if url == _PRIMARY_URL_10K:
            return 200, _PRIMARY_HTML_10K.encode(), _PRIMARY_HTML_10K
        return 404, b"", ""

    captured = _patch_httpx(monkeypatch, responder=_respond)

    s1 = await edgar.fetch(_INDEX_URL_10K)
    s2 = await edgar.fetch(_INDEX_URL_10K)

    assert s1 is not None
    assert s2 is not None
    # Index page is fetched both times (filings can have updated indexes),
    # but the primary doc is only fetched once thanks to the cache.
    primary_calls = [u for u in captured["urls"] if u == _PRIMARY_URL_10K]
    assert len(primary_calls) == 1


async def test_fetch_returns_none_on_404(monkeypatch, cache_dir: Path):
    def _respond(url: str):
        return 404, b"", "not found"

    _patch_httpx(monkeypatch, responder=_respond)

    assert await edgar.fetch(_INDEX_URL_10K) is None


async def test_fetch_returns_none_on_transport_error(monkeypatch, cache_dir: Path):
    @asynccontextmanager
    async def _client_factory(*args, **kwargs):
        class _Client:
            async def get(self, *_a, **_k):
                raise httpx.ConnectError("nope")

        yield _Client()

    monkeypatch.setattr(edgar.httpx, "AsyncClient", _client_factory)

    assert await edgar.fetch(_INDEX_URL_10K) is None


async def test_fetch_returns_none_on_unparseable_index(monkeypatch, cache_dir: Path):
    """When the index page has no document table, fetch must return None."""

    empty_body = "<html><body>nothing here</body></html>"

    def _respond(url: str):
        return 200, empty_body.encode(), empty_body

    _patch_httpx(monkeypatch, responder=_respond)

    assert await edgar.fetch(_INDEX_URL_10K) is None


async def test_fetch_user_agent_missing_email_raises(monkeypatch, cache_dir: Path):
    monkeypatch.setenv("RESEARCH_USER_AGENT", "no-email-here")

    with pytest.raises(RuntimeError, match="contact email"):
        await edgar.fetch(_INDEX_URL_10K)


# ---------------------------------------------------------------------------
# Smoke registration
# ---------------------------------------------------------------------------


def test_smoke_registry_includes_edgar():
    from research_agent.tools import TOOL_REGISTRY

    assert "edgar" in TOOL_REGISTRY


def test_smoke_edgar_skips_when_user_agent_missing(monkeypatch):
    """Issue #156: smoke must not raise when RESEARCH_USER_AGENT is unset.

    The default UA declared in EXPECTED_ENV_KEYS contains no ``@``, so the
    fallback is exercised here too — the wrapper must treat both unset env
    *and* the default placeholder as "no contact email".
    """
    from research_agent.tools import TOOL_REGISTRY

    monkeypatch.delenv("RESEARCH_USER_AGENT", raising=False)

    def _boom(*_a, **_k):
        raise AssertionError("edgar.search must not run when UA is missing")

    monkeypatch.setattr(edgar, "search", _boom)

    result = TOOL_REGISTRY["edgar"]("Cisco 8-K cybersecurity")

    assert isinstance(result, str)
    assert result.startswith("_smoke-tool edgar: would need RESEARCH_USER_AGENT")


def test_smoke_edgar_skips_when_user_agent_has_no_email(monkeypatch):
    """A UA without an `@` should also gracefully skip rather than hard-fail."""
    from research_agent.tools import TOOL_REGISTRY

    monkeypatch.setenv("RESEARCH_USER_AGENT", "research-agent no-email-here")

    def _boom(*_a, **_k):
        raise AssertionError("edgar.search must not run when UA has no email")

    monkeypatch.setattr(edgar, "search", _boom)

    result = TOOL_REGISTRY["edgar"]("Cisco 8-K cybersecurity")

    assert result.startswith("_smoke-tool edgar: would need RESEARCH_USER_AGENT")


def test_smoke_edgar_runs_live_call_when_user_agent_set(monkeypatch):
    """With a valid UA, the smoke wrapper invokes edgar.search and formats hits."""
    from datetime import UTC, datetime

    from research_agent.tools import TOOL_REGISTRY
    from research_agent.tools.models import SearchResult

    monkeypatch.setenv("RESEARCH_USER_AGENT", "research-agent test@example.com")

    called: dict[str, object] = {}

    async def _fake_search(query: str, *, form_type=None, max_results=20):
        called["query"] = query
        called["form_type"] = form_type
        called["max_results"] = max_results
        return [
            SearchResult(
                title="Cisco 8-K",
                url="https://www.sec.gov/Archives/edgar/data/858877/0000858877-23-000018-index.htm",
                snippet="Cybersecurity incident disclosure",
                source_kind="sec",
                published_at=datetime(2023, 8, 9, tzinfo=UTC),
                extras={"company": "Cisco Systems, Inc.", "form": "8-K"},
            )
        ]

    monkeypatch.setattr(edgar, "search", _fake_search)

    result = TOOL_REGISTRY["edgar"]("Cisco 8-K cybersecurity")

    assert called["query"] == "Cisco 8-K cybersecurity"
    assert called["form_type"] == "8-K"
    assert "Cisco Systems, Inc." in result
    assert "8-K" in result
    assert "2023-08-09" in result
