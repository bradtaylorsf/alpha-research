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


# ---------------------------------------------------------------------------
# Per-page extraction (issue #351 — dossier mode, epic #359)
# ---------------------------------------------------------------------------


def _stub_pypdf_pages(monkeypatch, pages: list[str]):
    """Install a pypdf stub whose reader returns the supplied per-page texts."""

    class _FakePage:
        def __init__(self, text: str) -> None:
            self._text = text

        def extract_text(self) -> str:
            return self._text

    class _FakeReader:
        def __init__(self, *_a, **_kw) -> None:
            self.pages = [_FakePage(t) for t in pages]
            self.outline = []

    fake_pypdf = type("M", (), {"PdfReader": _FakeReader})
    monkeypatch.setitem(__import__("sys").modules, "pypdf", fake_pypdf)


def test_extract_pages_sync_single_page_fixture():
    """Real single-page arxiv fixture: returns one tuple with 1-based page_no."""
    pages = pdf.extract_pages_sync(ARXIV_PDF)
    assert len(pages) == 1
    page_no, text = pages[0]
    assert page_no == 1
    assert "arxiv research paper text" in text


def test_extract_pages_sync_returns_distinct_per_page(monkeypatch):
    """Multi-page text-layer: every page's text is distinct, in order, no bleed."""
    _stub_pypdf_pages(
        monkeypatch,
        [
            "First page content about Alpha. " * 20,
            "Second page covers Beta in detail. " * 20,
            "Third page mentions Gamma topics. " * 20,
        ],
    )

    pages = pdf.extract_pages_sync(ARXIV_PDF)
    assert [n for n, _ in pages] == [1, 2, 3]
    assert "Alpha" in pages[0][1]
    assert "Beta" in pages[1][1]
    assert "Gamma" in pages[2][1]
    # Acceptance: per-page boundaries hold, no cross-page bleed.
    assert "Beta" not in pages[0][1]
    assert "Alpha" not in pages[1][1]
    assert "Gamma" not in pages[1][1]


def test_extract_pages_sync_respects_max_pages(monkeypatch):
    _stub_pypdf_pages(monkeypatch, [f"Page {i} body " * 30 for i in range(5)])
    pages = pdf.extract_pages_sync(ARXIV_PDF, max_pages=2)
    assert [n for n, _ in pages] == [1, 2]


def test_extract_pages_sync_truncates_per_page(monkeypatch):
    """max_chars is applied per page so callers see bounded entries."""
    _stub_pypdf_pages(monkeypatch, ["x" * 5_000, "y" * 5_000])
    monkeypatch.setattr(pdf, "_pages_density", lambda _pages: True)

    pages = pdf.extract_pages_sync(ARXIV_PDF, max_chars=100)
    assert len(pages) == 2
    for _, text in pages:
        body, _, _ = text.partition("…[truncated]")
        assert len(body.rstrip()) <= 100
        assert text.endswith("…[truncated]")


def test_extract_pages_sync_emits_empty_string_for_blank_pages(monkeypatch):
    """Sub-floor pages remain in the list so callers can correlate by index."""
    _stub_pypdf_pages(
        monkeypatch,
        ["Real content " * 30, "", "More content " * 30, ""],
    )

    pages = pdf.extract_pages_sync(ARXIV_PDF)
    assert [n for n, _ in pages] == [1, 2, 3, 4]
    assert pages[1][1] == ""
    assert pages[3][1] == ""


def test_extract_pages_sync_falls_through_to_ocr(monkeypatch):
    """Scanned PDF: pypdf empty, OCR fills every page, distinct per page."""
    monkeypatch.setattr(pdf, "_extract_pypdf_pages", lambda *a, **k: ["", ""])
    monkeypatch.setattr(pdf, "_extract_pdfplumber_pages", lambda *a, **k: ["", ""])
    monkeypatch.setattr(
        pdf,
        "_extract_tesseract_pages",
        lambda *a, **k: [
            "Scanned filing recovered via OCR " * 20,
            "Page two OCR distinct content " * 20,
        ],
    )

    pages = pdf.extract_pages_sync(SCANNED_PDF)
    assert len(pages) == 2
    assert [n for n, _ in pages] == [1, 2]
    assert "Scanned filing" in pages[0][1]
    assert "Page two OCR" in pages[1][1]
    # Cross-page bleed check on the OCR fall-through path too.
    assert "Page two OCR" not in pages[0][1]
    assert "Scanned filing" not in pages[1][1]


def test_extract_pages_sync_no_density_returns_best_layer(monkeypatch):
    """No layer meets the floor — return whichever produced the most text."""
    monkeypatch.setattr(pdf, "_extract_pypdf_pages", lambda *a, **k: [""])
    monkeypatch.setattr(
        pdf, "_extract_pdfplumber_pages", lambda *a, **k: ["short"]
    )
    monkeypatch.setattr(
        pdf,
        "_extract_tesseract_pages",
        lambda *a, **k: ["a much longer string that beats plumber"],
    )

    pages = pdf.extract_pages_sync(ARXIV_PDF)
    assert len(pages) == 1
    assert "much longer string" in pages[0][1]


def test_extract_pages_sync_returns_empty_when_every_layer_yields_nothing(
    monkeypatch,
):
    monkeypatch.setattr(pdf, "_extract_pypdf_pages", lambda *a, **k: [])
    monkeypatch.setattr(pdf, "_extract_pdfplumber_pages", lambda *a, **k: [])
    monkeypatch.setattr(pdf, "_extract_tesseract_pages", lambda *a, **k: [])

    assert pdf.extract_pages_sync(ARXIV_PDF) == []


def test_extract_pages_sync_hybrid_merges_text_and_ocr(monkeypatch):
    """Hybrid: text-layer + OCR supplement per page, no cross-page bleed."""
    monkeypatch.setattr(
        pdf,
        "_extract_pypdf_pages",
        lambda *a, **k: ["Text layer page one.", "Text layer page two."],
    )
    monkeypatch.setattr(pdf, "_extract_pdfplumber_pages", lambda *a, **k: [])
    monkeypatch.setattr(
        pdf,
        "_extract_tesseract_pages",
        lambda *a, **k: ["OCR alpha supplement.", "OCR beta supplement."],
    )

    pages = pdf.extract_pages_sync(ARXIV_PDF, hybrid_pages=True)
    assert [n for n, _ in pages] == [1, 2]
    p1, p2 = pages[0][1], pages[1][1]
    assert "Text layer page one." in p1
    assert "OCR alpha supplement." in p1
    assert "[OCR supplement]" in p1
    assert "Text layer page two." in p2
    assert "OCR beta supplement." in p2
    # The signal acceptance criterion: OCR for page N never appears in
    # any other page's slot.
    assert "OCR beta supplement" not in p1
    assert "OCR alpha supplement" not in p2
    assert "Text layer page two" not in p1
    assert "Text layer page one" not in p2


def test_extract_pages_sync_hybrid_text_only_when_ocr_unavailable(monkeypatch):
    """Hybrid with no OCR binary: text-only pages, no supplement marker."""
    monkeypatch.setattr(
        pdf,
        "_extract_pypdf_pages",
        lambda *a, **k: ["Text layer page one.", "Text layer page two."],
    )
    monkeypatch.setattr(pdf, "_extract_pdfplumber_pages", lambda *a, **k: [])
    monkeypatch.setattr(pdf, "_extract_tesseract_pages", lambda *a, **k: [])

    pages = pdf.extract_pages_sync(ARXIV_PDF, hybrid_pages=True)
    assert len(pages) == 2
    assert pages[0][1] == "Text layer page one."
    assert pages[1][1] == "Text layer page two."
    assert "[OCR supplement]" not in pages[0][1]


def test_extract_pages_sync_hybrid_ocr_only_when_text_blank(monkeypatch):
    """Hybrid with empty text layer falls back to OCR-only pages."""
    monkeypatch.setattr(pdf, "_extract_pypdf_pages", lambda *a, **k: ["", ""])
    monkeypatch.setattr(
        pdf, "_extract_pdfplumber_pages", lambda *a, **k: ["", ""]
    )
    monkeypatch.setattr(
        pdf,
        "_extract_tesseract_pages",
        lambda *a, **k: ["OCR alpha page one.", "OCR beta page two."],
    )

    pages = pdf.extract_pages_sync(ARXIV_PDF, hybrid_pages=True)
    assert len(pages) == 2
    assert pages[0][1] == "OCR alpha page one."
    assert pages[1][1] == "OCR beta page two."
    assert "[OCR supplement]" not in pages[0][1]


def test_extract_pages_sync_hybrid_prefers_plumber_when_pypdf_thin(monkeypatch):
    """When pypdf returns thin text but plumber recovers more, hybrid uses plumber."""
    monkeypatch.setattr(pdf, "_extract_pypdf_pages", lambda *a, **k: ["x", "y"])
    monkeypatch.setattr(
        pdf,
        "_extract_pdfplumber_pages",
        lambda *a, **k: [
            "Plumber recovered structured text " * 5,
            "Plumber page two structured " * 5,
        ],
    )
    monkeypatch.setattr(pdf, "_extract_tesseract_pages", lambda *a, **k: [])

    pages = pdf.extract_pages_sync(ARXIV_PDF, hybrid_pages=True)
    assert "Plumber recovered structured text" in pages[0][1]
    assert "Plumber page two structured" in pages[1][1]


def test_extract_pages_sync_url_routes_through_fetch(monkeypatch):
    captured: list[str] = []

    async def _fake_fetch(url, *, timeout=60.0):
        captured.append(url)
        return ARXIV_PDF.read_bytes()

    monkeypatch.setattr(pdf, "fetch_pdf_bytes", _fake_fetch)

    pages = pdf.extract_pages_sync("https://example.com/sample.pdf")
    assert captured == ["https://example.com/sample.pdf"]
    assert pages
    assert "arxiv research paper text" in pages[0][1]


def test_extract_pages_sync_returns_empty_for_missing_path():
    assert pdf.extract_pages_sync("/no/such/file.pdf") == []


async def test_extract_pages_async_wrapper(monkeypatch):
    """Async wrapper runs the same core in a thread executor."""
    _stub_pypdf_pages(monkeypatch, ["Async page body " * 30])
    pages = await pdf.extract_pages(ARXIV_PDF)
    assert pages[0][0] == 1
    assert "Async page body" in pages[0][1]


def test_extract_sync_regression_unchanged():
    """extract_sync() contract must stay green after the additive change."""
    text = pdf.extract_sync(ARXIV_PDF)
    assert "## Page 1" in text
    assert "arxiv research paper text" in text


def test_hybrid_merge_pages_zero_length_inputs():
    """Edge: empty lists merge to empty list."""
    assert pdf._hybrid_merge_pages([], []) == []


def test_hybrid_merge_pages_uneven_lengths():
    """Edge: when one list is shorter, the longer one's tail is preserved."""
    merged = pdf._hybrid_merge_pages(["A", "B", "C"], ["x"])
    assert merged[0] == "A\n\n[OCR supplement]\nx"
    assert merged[1] == "B"
    assert merged[2] == "C"


def test_pages_density_empty_returns_false():
    assert pdf._pages_density([]) is False
    assert pdf._pages_density(["", "", ""]) is False


def test_pages_density_passes_when_above_threshold():
    one_page = "a" * (pdf._DENSITY_MIN_ALPHA_PER_PAGE * 2)
    assert pdf._pages_density([one_page]) is True


# ---------------------------------------------------------------------------
# Section walk (issue #206)
# ---------------------------------------------------------------------------


def test_extract_sections_unstructured_falls_through_to_windows():
    """A PDF with no outline + few pages emits sliding-window pseudo-sections.

    The arxiv fixture is a single page with no bookmarks. After the outline
    + heading-regex paths fail to produce ≥3 sections, the function falls
    through to the sliding-window representation flagged with
    ``structured=False`` so the caller can dedupe by Jaccard.
    """
    sections = pdf.extract_sections_sync(ARXIV_PDF, doc_title="ArXiv Paper")
    assert sections, "extract_sections_sync produced no sections"
    assert all(not s["structured"] for s in sections)
    for section in sections:
        assert "ArXiv Paper" in section["breadcrumb"]
        assert "window" in section["breadcrumb"]
        assert isinstance(section["text"], str)
        assert section["text"].strip()


def test_extract_sections_uses_outline_when_available(monkeypatch):
    """Outline anchors with ≥3 entries drive the structural slice."""
    from pathlib import Path

    fake_pages = [f"Page {i + 1} body text " * 10 for i in range(8)]

    class _FakePage:
        def __init__(self, text: str) -> None:
            self._text = text

        def extract_text(self) -> str:
            return self._text

    class _FakeDest:
        def __init__(self, title: str) -> None:
            self.title = title

    class _FakeReader:
        def __init__(self, *_args, **_kwargs) -> None:
            self.outline = [
                _FakeDest("Chapter 1: Intro"),
                _FakeDest("Chapter 2: Methods"),
                _FakeDest("Chapter 3: Results"),
            ]
            self.pages = [_FakePage(t) for t in fake_pages]

        def get_destination_page_number(self, item) -> int:
            return {
                "Chapter 1: Intro": 0,
                "Chapter 2: Methods": 3,
                "Chapter 3: Results": 5,
            }[item.title]

    fake_pypdf = type("M", (), {"PdfReader": _FakeReader})
    monkeypatch.setitem(__import__("sys").modules, "pypdf", fake_pypdf)

    sections = pdf._build_sections(
        Path("/tmp/fake.pdf"),
        max_pages=20,
        max_chars_per_section=200_000,
        doc_title="Doc",
    )
    assert len(sections) == 3
    assert sections[0]["breadcrumb"].startswith("Doc > Chapter 1: Intro")
    assert sections[0]["page_start"] == 1
    assert sections[0]["page_end"] == 3
    assert sections[1]["page_start"] == 4
    assert sections[1]["page_end"] == 5
    assert sections[2]["page_end"] == 8
    assert all(s["structured"] is True for s in sections)


def test_slide_windows_overlaps():
    text = "abcdefghijklmnopqrstuvwxyz" * 1000  # 26_000 chars
    windows = pdf._slide_windows(text, window=10_000, overlap=1_000)
    assert len(windows) >= 3
    # Each window's start should advance by step = window - overlap = 9_000.
    starts = [w[0] for w in windows]
    assert starts[0] == 0
    assert starts[1] == 9_000
    # Ends are bounded by len(text).
    assert windows[-1][1] <= len(text)


def test_split_oversized_sections_replaces_with_windows():
    big_text = "X" * 350_000
    sections = [
        {
            "breadcrumb": "Doc > Big Chapter",
            "text": big_text,
            "page_start": 1,
            "page_end": 200,
            "structured": True,
        }
    ]
    out = pdf._split_oversized_sections(sections, max_chars_per_section=200_000)
    assert len(out) >= 2
    assert all("window" in s["breadcrumb"] for s in out)
    assert all(s["structured"] is False for s in out)
