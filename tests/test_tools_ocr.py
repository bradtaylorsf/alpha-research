"""Tests for `research_agent.tools.ocr` (issue #109)."""

from __future__ import annotations

from pathlib import Path

import pytest

from research_agent.tools import ocr, web_fetch

FIXTURES = Path(__file__).parent / "fixtures"
SCREENSHOT_PNG = FIXTURES / "screenshot.png"


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    monkeypatch.delenv("RESEARCH_OCR_VLM_ESCALATION", raising=False)
    monkeypatch.delenv("RESEARCH_IGNORE_ROBOTS", raising=False)
    web_fetch.reset_for_tests()
    yield
    web_fetch.reset_for_tests()


# ---------------------------------------------------------------------------
# Word-confidence math
# ---------------------------------------------------------------------------


def test_average_word_confidence_skips_minus_one_sentinels():
    """Tesseract emits -1 for non-word entries; folding them in skews the avg."""
    avg = ocr._average_word_confidence([-1, 80, 90, -1, 70])
    assert avg == pytest.approx(0.80)


def test_average_word_confidence_returns_zero_when_no_valid_entries():
    assert ocr._average_word_confidence([-1, -1, -1]) == 0.0
    assert ocr._average_word_confidence([]) == 0.0


def test_average_word_confidence_scales_to_unit_interval():
    """Tesseract reports 0–100; we hand callers 0.0–1.0 for threshold parity."""
    assert ocr._average_word_confidence([100, 100]) == pytest.approx(1.0)
    assert ocr._average_word_confidence([0]) == 0.0


# ---------------------------------------------------------------------------
# Layer 1 — Tesseract
# ---------------------------------------------------------------------------


def test_tesseract_skipped_when_binary_missing(monkeypatch):
    monkeypatch.setattr(ocr, "_tesseract_available", lambda: False)
    text, conf = ocr._extract_tesseract(SCREENSHOT_PNG, conf_threshold=0.7)
    assert (text, conf) == ("", 0.0)


def test_tesseract_returns_text_and_avg_conf(monkeypatch):
    """Layer wires PIL → pytesseract.image_to_data; assert without a real binary.

    Patch the availability probe and the OCR call so the layer can be
    exercised end-to-end on hosts that don't have ``tesseract`` installed.
    """
    monkeypatch.setattr(ocr, "_tesseract_available", lambda: True)

    captured: list[object] = []

    class _FakePytesseract:
        class Output:
            DICT = "dict"

        @staticmethod
        def image_to_data(image, output_type=None):
            captured.append((image, output_type))
            return {
                "text": ["", "Hello", "world", ""],
                "conf": [-1, 92, 88, -1],
            }

    monkeypatch.setitem(__import__("sys").modules, "pytesseract", _FakePytesseract)

    text, conf = ocr._extract_tesseract(SCREENSHOT_PNG, conf_threshold=0.7)
    assert "Hello" in text
    assert "world" in text
    assert conf == pytest.approx(0.90)
    assert captured, "image_to_data was never invoked"


def test_tesseract_low_confidence_returns_text_below_threshold(monkeypatch):
    """A low-confidence pass still returns the text — pipeline gate decides."""
    monkeypatch.setattr(ocr, "_tesseract_available", lambda: True)

    class _FakePytesseract:
        class Output:
            DICT = "dict"

        @staticmethod
        def image_to_data(image, output_type=None):
            return {
                "text": ["maybe", "garbled"],
                "conf": [40, 35],
            }

    monkeypatch.setitem(__import__("sys").modules, "pytesseract", _FakePytesseract)

    text, conf = ocr._extract_tesseract(SCREENSHOT_PNG, conf_threshold=0.7)
    assert "maybe" in text
    assert conf == pytest.approx(0.375)


# ---------------------------------------------------------------------------
# Pipeline — _extract_sync gates
# ---------------------------------------------------------------------------


def test_pipeline_returns_tesseract_when_confident(monkeypatch, tmp_path):
    """High-confidence Tesseract result must short-circuit before any VLM."""
    src = tmp_path / "img.png"
    src.write_bytes(b"\x00")

    monkeypatch.setattr(
        ocr,
        "_extract_tesseract",
        lambda path, conf_threshold: ("Hello world", 0.95),
    )

    def _vlm_should_not_run(*args, **kwargs):
        raise AssertionError("local VLM should not run when Tesseract is confident")

    monkeypatch.setattr(ocr, "_extract_local_vlm", _vlm_should_not_run)
    monkeypatch.setattr(ocr, "_extract_vlm", _vlm_should_not_run)

    text = ocr._extract_sync(
        src, max_chars=ocr.DEFAULT_MAX_CHARS, conf_threshold=0.7,
        source_label=str(src), job=None,
    )
    assert "Hello world" in text


def test_pipeline_falls_through_to_local_vlm_on_low_confidence(monkeypatch, tmp_path):
    """conf below threshold must hand off to the local VLM."""
    src = tmp_path / "img.png"
    src.write_bytes(b"\x00")

    monkeypatch.setattr(
        ocr,
        "_extract_tesseract",
        lambda path, conf_threshold: ("garbled output", 0.3),
    )

    invocations: list[Path] = []

    def _fake_local(path):
        invocations.append(path)
        return "## Recovered\n\nlocal VLM transcription"

    monkeypatch.setattr(ocr, "_extract_local_vlm", _fake_local)

    def _no_cloud(*args, **kwargs):
        raise AssertionError("cloud VLM must not run when escalation flag is unset")

    monkeypatch.setattr(ocr, "_extract_vlm", _no_cloud)

    text = ocr._extract_sync(
        src, max_chars=ocr.DEFAULT_MAX_CHARS, conf_threshold=0.7,
        source_label=str(src), job=None,
    )
    assert "local VLM transcription" in text
    assert invocations == [src]


def test_pipeline_skips_local_vlm_when_tesseract_empty_but_local_returns_text(
    monkeypatch, tmp_path,
):
    """Empty Tesseract → local VLM still runs; non-empty local result wins."""
    src = tmp_path / "img.png"
    src.write_bytes(b"\x00")

    monkeypatch.setattr(ocr, "_extract_tesseract", lambda path, conf_threshold: ("", 0.0))
    monkeypatch.setattr(ocr, "_extract_local_vlm", lambda path: "local result")

    def _no_cloud(*args, **kwargs):
        raise AssertionError("cloud VLM must not run when escalation flag is unset")

    monkeypatch.setattr(ocr, "_extract_vlm", _no_cloud)

    text = ocr._extract_sync(
        src, max_chars=ocr.DEFAULT_MAX_CHARS, conf_threshold=0.7,
        source_label=str(src), job=None,
    )
    assert text == "local result"


# ---------------------------------------------------------------------------
# Layer 3 — VLM escalation gating
# ---------------------------------------------------------------------------


def test_vlm_escalation_disabled_by_default(monkeypatch, tmp_path):
    """The Opus tier is gated — never invoked unless the env flag is set."""
    src = tmp_path / "img.png"
    src.write_bytes(b"\x00")

    monkeypatch.setattr(ocr, "_extract_tesseract", lambda path, conf_threshold: ("", 0.0))
    monkeypatch.setattr(ocr, "_extract_local_vlm", lambda path: "")

    def _boom(*args, **kwargs):
        raise AssertionError("VLM escalation should be disabled")

    monkeypatch.setattr(ocr, "_run_vlm_call", _boom)

    text = ocr._extract_sync(
        src, max_chars=ocr.DEFAULT_MAX_CHARS, conf_threshold=0.7,
        source_label=str(src), job=None,
    )
    assert text == ""


def test_vlm_escalation_runs_when_env_set(monkeypatch, tmp_path):
    monkeypatch.setenv("RESEARCH_OCR_VLM_ESCALATION", "1")

    src = tmp_path / "img.png"
    src.write_bytes(b"\x00")

    monkeypatch.setattr(ocr, "_extract_tesseract", lambda path, conf_threshold: ("", 0.0))
    monkeypatch.setattr(ocr, "_extract_local_vlm", lambda path: "")

    invocations: list[Path] = []

    def _fake_run_vlm(path):
        invocations.append(path)
        return "## Recovered\n\nfrontier vision tier output"

    monkeypatch.setattr(ocr, "_run_vlm_call", _fake_run_vlm)

    text = ocr._extract_sync(
        src, max_chars=ocr.DEFAULT_MAX_CHARS, conf_threshold=0.7,
        source_label=str(src), job=None,
    )
    assert "frontier vision tier output" in text
    assert invocations == [src]


def test_vlm_escalation_emits_warn_event_before_call(monkeypatch, tmp_path):
    """Operators rely on the WARN to see when paid escalation fires."""
    from research_agent.observability import events
    from research_agent.storage import db
    from research_agent.storage.jobs import Job

    monkeypatch.setenv("RESEARCH_OCR_VLM_ESCALATION", "1")

    db_path = tmp_path / "index.sqlite"
    db.migrate(path=db_path).close()
    job = Job.create(
        {"goal": "ocr vlm test"},
        jobs_root=tmp_path / "jobs",
        db_path=db_path,
    )

    src = tmp_path / "img.png"
    src.write_bytes(b"\x89PNG\r\n")

    monkeypatch.setattr(ocr, "_extract_tesseract", lambda path, conf_threshold: ("", 0.0))
    monkeypatch.setattr(ocr, "_extract_local_vlm", lambda path: "")
    monkeypatch.setattr(ocr, "_run_vlm_call", lambda path: "frontier-vision recovered")

    out = ocr.extract_sync(src, job=job)
    assert "frontier-vision recovered" in out

    lines = (job.root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    parsed = [events.Event.model_validate_json(line) for line in lines if line.strip()]
    kinds = [ev.kind for ev in parsed]
    assert "ocr_vlm_escalation" in kinds
    warn = next(ev for ev in parsed if ev.kind == "ocr_vlm_escalation")
    assert warn.level == "WARN"
    assert warn.payload["model"] == "anthropic/claude-opus-4-7"


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def test_truncate_marks_when_over_cap():
    out = ocr._truncate("x" * 1_000, 100)
    assert out.endswith("…[truncated]")
    body, _, _ = out.partition("…[truncated]")
    assert len(body.rstrip()) <= 100


def test_truncate_noop_under_cap():
    assert ocr._truncate("hi", 100) == "hi"


# ---------------------------------------------------------------------------
# URL fetch path
# ---------------------------------------------------------------------------


async def test_extract_url_path_fetches_via_httpx(monkeypatch):
    """A URL must round-trip through fetch_image_bytes → temp file → pipeline."""
    captured_urls: list[str] = []
    captured_paths: list[Path] = []

    async def _fake_fetch(url, *, timeout=60.0):
        captured_urls.append(url)
        return SCREENSHOT_PNG.read_bytes()

    def _fake_extract_sync(path, max_chars, conf_threshold, source_label, job):
        captured_paths.append(path)
        return "url-extracted markdown"

    monkeypatch.setattr(ocr, "fetch_image_bytes", _fake_fetch)
    monkeypatch.setattr(ocr, "_extract_sync", _fake_extract_sync)

    text = await ocr.extract("https://example.com/screenshot.png")
    assert text == "url-extracted markdown"
    assert captured_urls == ["https://example.com/screenshot.png"]
    # The temp file the URL fetch created must be unlinked by the time we return.
    assert captured_paths
    assert not captured_paths[0].exists()
    # Suffix must be derived from the URL path.
    assert captured_paths[0].suffix == ".png"


async def test_extract_returns_empty_on_missing_path():
    assert await ocr.extract("/no/such/file.png") == ""


def test_extract_from_bytes_empty_input():
    assert ocr.extract_from_bytes(b"") == ""


def test_extract_from_bytes_routes_through_pipeline(monkeypatch):
    captured: list[Path] = []

    def _fake(path, max_chars, conf_threshold, source_label, job):
        captured.append(path)
        return "from bytes"

    monkeypatch.setattr(ocr, "_extract_sync", _fake)

    out = ocr.extract_from_bytes(b"\x89PNG\r\n", suffix=".png")
    assert out == "from bytes"
    assert captured and captured[0].suffix == ".png"
    # The temp file must be cleaned up before we return.
    assert not captured[0].exists()


def test_suffix_for_url_picks_extension():
    assert ocr._suffix_for("https://x.example/img.png") == ".png"
    assert ocr._suffix_for("https://x.example/img.JPG") == ".jpg"
    assert ocr._suffix_for("https://x.example/img.webp?token=abc") == ".webp"
    # Unknown suffix falls back to .png.
    assert ocr._suffix_for("https://x.example/no-suffix") == ".png"


# ---------------------------------------------------------------------------
# web_fetch routing
# ---------------------------------------------------------------------------


def test_is_image_url_handles_query_string():
    assert web_fetch._is_image_url("https://x.example/shot.png?token=abc") is True
    assert web_fetch._is_image_url("https://x.example/photo.JPG") is True
    assert web_fetch._is_image_url("https://x.example/index.html") is False


def test_is_image_content_type_strips_charset():
    assert web_fetch._is_image_content_type("image/png") is True
    assert web_fetch._is_image_content_type("image/jpeg; charset=binary") is True
    assert web_fetch._is_image_content_type("text/html") is False
    assert web_fetch._is_image_content_type(None) is False


async def test_web_fetch_routes_image_url_through_ocr_extract(monkeypatch):
    """A ``.png`` URL must bypass trafilatura and land at ocr.extract."""
    monkeypatch.setenv("RESEARCH_IGNORE_ROBOTS", "1")

    async def _no_archive(url, timeout: float = 30.0):
        return None

    monkeypatch.setattr(web_fetch.archive, "save", _no_archive)

    fetched: list[str] = []

    async def _fake_extract(url, *, max_chars=200_000, conf_threshold=0.7, job=None):
        fetched.append(url)
        return "## Screenshot\n\nrouted-via-ocr text"

    monkeypatch.setattr(ocr, "extract", _fake_extract)

    async def _explode(*args, **kwargs):
        raise AssertionError("httpx should be skipped for image URLs")

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _explode)

    source = await web_fetch.fetch("https://example.com/shot.png")
    assert source is not None
    assert source.source_kind == "image"
    assert source.metadata["fetched_via"] == "ocr"
    assert "routed-via-ocr text" in source.cleaned_text
    assert fetched == ["https://example.com/shot.png"]


async def test_web_fetch_routes_image_content_type(monkeypatch):
    """Server-declared image (no recognisable suffix) routes via extract_from_bytes."""
    monkeypatch.setenv("RESEARCH_IGNORE_ROBOTS", "1")

    async def _no_archive(url, timeout: float = 30.0):
        return None

    monkeypatch.setattr(web_fetch.archive, "save", _no_archive)

    fake_bytes = b"\x89PNG\r\nfake-bytes"

    async def _fake_httpx(url, timeout, user_agent):
        return 200, "garbage", fake_bytes, "image/png"

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _fake_httpx)

    consumed: list[bytes] = []

    def _fake_extract_from_bytes(data, *, suffix=".png", source_label="<bytes>", **kwargs):
        consumed.append(data)
        return "routed-via-bytes"

    monkeypatch.setattr(ocr, "extract_from_bytes", _fake_extract_from_bytes)

    source = await web_fetch.fetch("https://example.com/download")
    assert source is not None
    assert source.source_kind == "image"
    assert source.metadata["fetched_via"] == "ocr"
    assert source.metadata["status_code"] == 200
    assert consumed == [fake_bytes]


# ---------------------------------------------------------------------------
# Smoke registry wiring
# ---------------------------------------------------------------------------


def test_tool_registry_has_ocr():
    from research_agent.tools import TOOL_REGISTRY

    assert "ocr" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["ocr"])


def test_smoke_ocr_summary_matches_contract(monkeypatch, tmp_path):
    """The smoke verb summary must include source / char_count / preview."""
    src = tmp_path / "img.png"
    src.write_bytes(b"\x00")

    def _fake_extract_sync(path_or_url, **kwargs):
        return "extracted markdown content here"

    monkeypatch.setattr(ocr, "extract_sync", _fake_extract_sync)

    from research_agent.tools import _smoke_ocr

    out = _smoke_ocr(str(src))
    assert f"source: {src}" in out
    assert "char_count:" in out
    assert "preview:" in out
