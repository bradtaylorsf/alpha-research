"""Tests for `research_agent.tools.pdf` (issue #108)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from research_agent.tools import pdf, web_fetch

FIXTURES = Path(__file__).parent / "fixtures"
ARXIV_PDF = FIXTURES / "arxiv_paper.pdf"
SCANNED_PDF = FIXTURES / "scanned.pdf"


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    monkeypatch.delenv("RESEARCH_PDF_VLM_ESCALATION", raising=False)
    monkeypatch.delenv("RESEARCH_IGNORE_ROBOTS", raising=False)
    web_fetch.reset_for_tests()
    yield
    web_fetch.reset_for_tests()


# ---------------------------------------------------------------------------
# Density gate + truncation helpers
# ---------------------------------------------------------------------------


def test_text_density_zero_pages_returns_false():
    assert pdf._text_density("plenty of text here", 0) is False


def test_text_density_passes_when_above_threshold():
    text = "a" * (pdf._DENSITY_MIN_ALPHA_PER_PAGE * 2)
    assert pdf._text_density(text, 1) is True


def test_text_density_fails_when_below_threshold():
    text = "a" * 10
    assert pdf._text_density(text, 1) is False


def test_truncate_marks_when_over_cap():
    out = pdf._truncate("x" * 1_000, 100)
    assert out.endswith("…[truncated]")
    assert len(out) > 100  # marker added


def test_truncate_noop_under_cap():
    assert pdf._truncate("hi", 100) == "hi"


# ---------------------------------------------------------------------------
# Layer 1 — pypdf path
# ---------------------------------------------------------------------------


async def test_extract_arxiv_fixture_via_pypdf():
    text = await pdf.extract(ARXIV_PDF)
    assert text
    assert "## Page 1" in text
    assert "arxiv research paper text" in text


async def test_extract_respects_max_pages_kwarg():
    """Even though the fixture is 1 page, the cap is plumbed end-to-end."""
    text = await pdf.extract(ARXIV_PDF, max_pages=1)
    assert "## Page 1" in text
    assert "## Page 2" not in text


async def test_extract_truncates_on_max_chars():
    text = await pdf.extract(ARXIV_PDF, max_chars=20)
    assert text.endswith("…[truncated]")
    # Truncation marker is allowed to push past max_chars by a constant; the
    # important contract is that the body before the marker is bounded.
    body, _, _ = text.partition("…[truncated]")
    assert len(body.rstrip()) <= 20


def test_extract_pypdf_returns_empty_for_image_pdf():
    """The scanned fixture has no extractable text — pypdf returns an empty body."""
    md, pages = pdf._extract_pypdf(SCANNED_PDF, max_pages=10)
    assert pages == 1
    # No "## Page" heading because pypdf produced no usable text.
    assert "## Page" not in md


# ---------------------------------------------------------------------------
# Layer 2 — pdfplumber path
# ---------------------------------------------------------------------------


def test_pdfplumber_renders_arxiv_fixture():
    md, pages = pdf._extract_pdfplumber(ARXIV_PDF, max_pages=10)
    assert pages == 1
    assert "## Page 1" in md
    assert "arxiv research paper text" in md


def test_table_to_markdown_renders_pipe_table():
    out = pdf._table_to_markdown([["Year", "Revenue"], ["2024", "$10"]])
    lines = out.splitlines()
    assert lines[0] == "| Year | Revenue |"
    assert lines[1] == "|---|---|"
    assert lines[2] == "| 2024 | $10 |"


def test_table_to_markdown_handles_empty():
    assert pdf._table_to_markdown([]) == ""
    assert pdf._table_to_markdown([[None, None]]) == "|  |  |\n|---|---|"


# ---------------------------------------------------------------------------
# Layer 3 — Tesseract fallback
# ---------------------------------------------------------------------------


def test_tesseract_skipped_when_binary_missing(monkeypatch):
    monkeypatch.setattr(pdf, "_tesseract_available", lambda: False)
    md, pages = pdf._extract_tesseract(SCANNED_PDF, max_pages=2)
    assert (md, pages) == ("", 0)


def test_tesseract_runs_when_binary_present(monkeypatch):
    """Layer 3 wires pypdfium2 → pytesseract.image_to_string.

    We don't need a real ``tesseract`` install for this assertion — patch
    both the availability probe and the OCR call so we can verify the
    rasterised pages reach pytesseract and the markdown gets stitched
    together correctly.
    """
    monkeypatch.setattr(pdf, "_tesseract_available", lambda: True)

    captured: list[object] = []

    class _FakePytesseract:
        @staticmethod
        def image_to_string(image):
            captured.append(image)
            return "Scanned filing\nThis fixture contains only an image of text."

    monkeypatch.setitem(__import__("sys").modules, "pytesseract", _FakePytesseract)

    md, pages = pdf._extract_tesseract(SCANNED_PDF, max_pages=2)
    assert pages == 1
    assert captured, "pytesseract.image_to_string was never called"
    assert "## Page 1" in md
    assert "Scanned filing" in md


def test_extract_falls_through_to_tesseract_for_image_pdf(monkeypatch):
    """Density gate forces pypdf/pdfplumber failure on the scanned fixture."""
    monkeypatch.setattr(pdf, "_tesseract_available", lambda: True)

    class _FakePytesseract:
        @staticmethod
        def image_to_string(image):
            return "Scanned filing recovered via OCR " * 20  # passes density

    monkeypatch.setitem(__import__("sys").modules, "pytesseract", _FakePytesseract)

    text = asyncio.run(pdf.extract(SCANNED_PDF))
    assert "Scanned filing recovered via OCR" in text
    assert "## Page 1" in text


# ---------------------------------------------------------------------------
# Layer 4 — VLM escalation
# ---------------------------------------------------------------------------


async def test_vlm_escalation_disabled_by_default(monkeypatch):
    """The Opus tier is gated — we never invoke the router unless the env
    flag is set, even when every cheaper layer fails the density gate."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("VLM escalation should be disabled")

    monkeypatch.setattr(pdf, "_run_vlm_call", _boom)
    # Force every cheaper layer to fail the density gate.
    monkeypatch.setattr(pdf, "_extract_pypdf", lambda *a, **k: ("", 0))
    monkeypatch.setattr(pdf, "_extract_pdfplumber", lambda *a, **k: ("", 0))
    monkeypatch.setattr(pdf, "_extract_tesseract", lambda *a, **k: ("", 0))

    text = await pdf.extract(SCANNED_PDF)
    assert text == ""


async def test_vlm_escalation_runs_when_env_set(monkeypatch):
    monkeypatch.setenv("RESEARCH_PDF_VLM_ESCALATION", "1")
    monkeypatch.setattr(pdf, "_extract_pypdf", lambda *a, **k: ("", 0))
    monkeypatch.setattr(pdf, "_extract_pdfplumber", lambda *a, **k: ("", 0))
    monkeypatch.setattr(pdf, "_extract_tesseract", lambda *a, **k: ("", 0))

    invocations: list[int] = []

    def _fake_vlm_call(images):
        invocations.append(len(images))
        return "## Page 1\n\nrecovered by frontier vision tier"

    monkeypatch.setattr(pdf, "_run_vlm_call", _fake_vlm_call)

    text = await pdf.extract(SCANNED_PDF)
    assert "recovered by frontier vision tier" in text
    assert invocations and invocations[0] >= 1


def test_vlm_escalation_emits_warn_event_before_call(monkeypatch, tmp_path):
    """Operators rely on the WARN to see when paid escalation fires."""
    from research_agent.observability import events
    from research_agent.storage import db
    from research_agent.storage.jobs import Job

    monkeypatch.setenv("RESEARCH_PDF_VLM_ESCALATION", "1")

    db_path = tmp_path / "index.sqlite"
    db.migrate(path=db_path).close()
    job = Job.create(
        {"goal": "pdf vlm test"},
        jobs_root=tmp_path / "jobs",
        db_path=db_path,
    )

    monkeypatch.setattr(pdf, "_extract_pypdf", lambda *a, **k: ("", 0))
    monkeypatch.setattr(pdf, "_extract_pdfplumber", lambda *a, **k: ("", 0))
    monkeypatch.setattr(pdf, "_extract_tesseract", lambda *a, **k: ("", 0))
    monkeypatch.setattr(
        pdf,
        "_run_vlm_call",
        lambda images: "## Page 1\n\nrecovered",
    )

    out = pdf.extract_sync(SCANNED_PDF, job=job)
    assert "recovered" in out

    # Read events.jsonl and confirm the kind landed.
    lines = (job.root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    parsed = [events.Event.model_validate_json(line) for line in lines if line.strip()]
    kinds = [ev.kind for ev in parsed]
    assert "pdf_vlm_escalation" in kinds
    warn_event = next(ev for ev in parsed if ev.kind == "pdf_vlm_escalation")
    assert warn_event.level == "WARN"
    assert warn_event.payload["pages_sent"] >= 1


# ---------------------------------------------------------------------------
# web_fetch routing
# ---------------------------------------------------------------------------


async def test_web_fetch_routes_pdf_url_to_pdf_extract(monkeypatch):
    """A ``.pdf`` URL must bypass trafilatura and land at pdf.extract."""
    monkeypatch.setenv("RESEARCH_IGNORE_ROBOTS", "1")

    async def _no_archive(url, timeout: float = 30.0):
        return None

    monkeypatch.setattr(web_fetch.archive, "save", _no_archive)

    fetched: list[str] = []

    async def _fake_pdf_extract(url, *, max_pages=100, max_chars=200_000, job=None):
        fetched.append(url)
        return "## Page 1\n\nrouted-via-pdf body that is long enough"

    # Patch the symbol the way web_fetch imports it (lazy import inside
    # _build_pdf_source).
    monkeypatch.setattr(pdf, "extract", _fake_pdf_extract)

    # Httpx must not be hit — the URL-suffix shortcut bypasses it.
    async def _explode(*args, **kwargs):
        raise AssertionError("httpx should be skipped for .pdf URLs")

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _explode)

    source = await web_fetch.fetch("https://www.sec.gov/Archives/x.pdf")
    assert source is not None
    assert source.source_kind == "pdf"
    assert source.metadata["fetched_via"] == "pdf"
    assert "routed-via-pdf body" in source.cleaned_text
    assert fetched == ["https://www.sec.gov/Archives/x.pdf"]


async def test_web_fetch_routes_application_pdf_content_type(monkeypatch):
    """Server-declared PDF (no ``.pdf`` suffix) should still route via pdf.extract."""
    monkeypatch.setenv("RESEARCH_IGNORE_ROBOTS", "1")

    async def _no_archive(url, timeout: float = 30.0):
        return None

    monkeypatch.setattr(web_fetch.archive, "save", _no_archive)

    fake_bytes = b"%PDF-fake"

    async def _fake_httpx(url, timeout, user_agent):
        return 200, "garbage", fake_bytes, "application/pdf"

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _fake_httpx)

    consumed: list[bytes] = []

    def _fake_extract_from_bytes(data, *, source_label="<bytes>", **kwargs):
        consumed.append(data)
        return "## Page 1\n\nrouted-via-bytes body that is more than five hundred chars " * 10

    monkeypatch.setattr(pdf, "extract_from_bytes", _fake_extract_from_bytes)

    source = await web_fetch.fetch("https://example.com/download")
    assert source is not None
    assert source.source_kind == "pdf"
    assert source.metadata["fetched_via"] == "pdf"
    assert source.metadata["status_code"] == 200
    assert consumed == [fake_bytes]


def test_is_pdf_url_handles_query_string():
    assert web_fetch._is_pdf_url("https://x.example/f.pdf?token=abc") is True
    assert web_fetch._is_pdf_url("https://x.example/F.PDF") is True
    assert web_fetch._is_pdf_url("https://x.example/index.html") is False


def test_is_pdf_content_type_strips_charset():
    assert web_fetch._is_pdf_content_type("application/pdf") is True
    assert web_fetch._is_pdf_content_type("application/pdf; charset=binary") is True
    assert web_fetch._is_pdf_content_type("text/html") is False
    assert web_fetch._is_pdf_content_type(None) is False


# ---------------------------------------------------------------------------
# extract_from_bytes + URL fetch
# ---------------------------------------------------------------------------


def test_extract_from_bytes_runs_layered_pipeline():
    data = ARXIV_PDF.read_bytes()
    text = pdf.extract_from_bytes(data)
    assert "arxiv research paper text" in text


def test_extract_from_bytes_empty_input():
    assert pdf.extract_from_bytes(b"") == ""


async def test_extract_url_path_fetches_via_httpx(monkeypatch):
    captured: list[str] = []

    async def _fake_fetch(url, *, timeout=60.0):
        captured.append(url)
        return ARXIV_PDF.read_bytes()

    monkeypatch.setattr(pdf, "fetch_pdf_bytes", _fake_fetch)

    text = await pdf.extract("https://example.com/sample.pdf")
    assert captured == ["https://example.com/sample.pdf"]
    assert "arxiv research paper text" in text


async def test_extract_returns_empty_on_missing_path():
    assert await pdf.extract("/no/such/file.pdf") == ""


# ---------------------------------------------------------------------------
# Smoke registry wiring
# ---------------------------------------------------------------------------


def test_tool_registry_has_pdf():
    from research_agent.tools import TOOL_REGISTRY

    assert "pdf" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["pdf"])


def test_smoke_pdf_against_local_fixture():
    from research_agent.tools import _smoke_pdf

    out = _smoke_pdf(str(ARXIV_PDF))
    assert "page_sections: 1" in out
    assert "preview:" in out
