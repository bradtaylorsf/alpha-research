"""Tests for `research_agent.tools.audio` (issue #110)."""

from __future__ import annotations

from pathlib import Path

import pytest

from research_agent.tools import audio, web_fetch


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch):
    monkeypatch.delenv("RESEARCH_IGNORE_ROBOTS", raising=False)
    web_fetch.reset_for_tests()
    yield
    web_fetch.reset_for_tests()


# ---------------------------------------------------------------------------
# Backend / format helpers
# ---------------------------------------------------------------------------


def test_supported_extensions_cover_common_formats():
    assert ".mp3" in audio.SUPPORTED_AUDIO_EXTENSIONS
    assert ".m4a" in audio.SUPPORTED_AUDIO_EXTENSIONS
    assert ".wav" in audio.SUPPORTED_AUDIO_EXTENSIONS
    assert ".ogg" in audio.SUPPORTED_AUDIO_EXTENSIONS
    assert ".flac" in audio.SUPPORTED_AUDIO_EXTENSIONS


def test_audio_content_types_cover_common_mimes():
    assert "audio/mpeg" in audio.AUDIO_CONTENT_TYPES
    assert "audio/wav" in audio.AUDIO_CONTENT_TYPES
    assert "audio/ogg" in audio.AUDIO_CONTENT_TYPES
    assert "audio/flac" in audio.AUDIO_CONTENT_TYPES


def test_format_seconds():
    assert audio._format_seconds(0) == "00:00"
    assert audio._format_seconds(65) == "01:05"
    assert audio._format_seconds(1800) == "30:00"


def test_suffix_for_url_picks_extension():
    assert audio._suffix_for("https://x.example/cast.mp3") == ".mp3"
    assert audio._suffix_for("https://x.example/cast.M4A") == ".m4a"
    assert audio._suffix_for("https://x.example/cast.wav?token=abc") == ".wav"
    # Unknown suffix falls back to mp3.
    assert audio._suffix_for("https://x.example/stream") == ".mp3"


def test_segments_to_lines_with_speaker_labels():
    segments = [
        {"text": "hello world", "speaker": "1"},
        {"text": "second line", "speaker": "2"},
    ]
    lines = audio._segments_to_lines(segments)
    assert lines == ["[Speaker 1] hello world", "[Speaker 2] second line"]


def test_segments_to_lines_without_speaker_labels():
    segments = [{"text": "no diarization here"}, {"text": "  "}, {"text": "second"}]
    lines = audio._segments_to_lines(segments)
    assert lines == ["[Speaker] no diarization here", "[Speaker] second"]


def test_render_markdown_emits_chunk_headings():
    out = audio._render_markdown(
        [["[Speaker] one"], ["[Speaker] two"]],
        chunk_seconds=1800,
    )
    assert "## Chunk 1 (00:00-30:00)" in out
    assert "## Chunk 2 (30:00-60:00)" in out
    assert "[Speaker] one" in out
    assert "[Speaker] two" in out


def test_render_markdown_handles_empty_chunk():
    out = audio._render_markdown([[]], chunk_seconds=1800)
    assert "## Chunk 1" in out
    assert "(no transcript produced for this chunk)" in out


# ---------------------------------------------------------------------------
# Chunk math — _chunk_audio
# ---------------------------------------------------------------------------


def test_chunk_audio_falls_back_when_ffmpeg_missing(monkeypatch, tmp_path):
    """Without ffmpeg the function must return the original path untouched."""
    monkeypatch.setattr(audio, "_ffmpeg_available", lambda: False)
    src = tmp_path / "input.mp3"
    src.write_bytes(b"\x00")
    chunks = audio._chunk_audio(src, chunk_seconds=1800)
    assert chunks == [src]


def test_chunk_audio_65_minute_input_yields_three_chunks(monkeypatch, tmp_path):
    """A 65-min input chunked at 30-min boundaries → 3 chunks (30+30+5).

    We stub the ffmpeg subprocess so the test stays offline-fast: the stub
    creates three pre-named output files in the dir ffmpeg was about to
    populate, mirroring what the real call would emit.
    """
    monkeypatch.setattr(audio, "_ffmpeg_available", lambda: True)

    captured_cmds: list[list[str]] = []

    def _fake_run(cmd, check, capture_output):
        captured_cmds.append(cmd)
        # The output pattern is the last argument: ".../chunk_%03d.wav".
        pattern_arg = cmd[-1]
        out_dir = Path(pattern_arg).parent
        for idx in range(3):
            (out_dir / f"chunk_{idx:03d}.wav").write_bytes(b"\x00\x00")

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(audio.subprocess, "run", _fake_run)

    src = tmp_path / "long.mp3"
    src.write_bytes(b"\x00")
    chunks = audio._chunk_audio(src, chunk_seconds=1800)

    assert len(chunks) == 3
    for chunk in chunks:
        assert chunk.exists()
    assert captured_cmds, "ffmpeg should have been invoked"
    assert "-segment_time" in captured_cmds[0]
    assert "1800" in captured_cmds[0]


def test_chunk_audio_falls_back_when_ffmpeg_fails(monkeypatch, tmp_path):
    """If ffmpeg returns non-zero we must not lose the original — fall back to whole file."""
    import subprocess as real_subprocess

    monkeypatch.setattr(audio, "_ffmpeg_available", lambda: True)

    def _boom(cmd, check, capture_output):
        raise real_subprocess.CalledProcessError(returncode=1, cmd=cmd)

    monkeypatch.setattr(audio.subprocess, "run", _boom)

    src = tmp_path / "input.mp3"
    src.write_bytes(b"\x00")
    chunks = audio._chunk_audio(src, chunk_seconds=1800)
    assert chunks == [src]


# ---------------------------------------------------------------------------
# transcribe() happy path
# ---------------------------------------------------------------------------


async def test_transcribe_happy_path_emits_chunk_and_speaker_markdown(monkeypatch, tmp_path):
    """End-to-end: file → chunked → backend → markdown with `## Chunk` + `[Speaker]`."""
    src = tmp_path / "podcast.mp3"
    src.write_bytes(b"\x00")

    monkeypatch.setattr(audio, "_chunk_audio", lambda path, chunk_seconds=1800: [path])

    def _fake_mlx(path, model):
        return [
            {"text": "first segment of speech", "speaker": "1"},
            {"text": "second segment", "speaker": "2"},
        ]

    monkeypatch.setattr(audio, "_transcribe_with_mlx", _fake_mlx)
    monkeypatch.setattr(audio, "_is_apple_silicon", lambda: True)

    text = await audio.transcribe(src)
    assert "## Chunk 1" in text
    assert "[Speaker 1] first segment" in text
    assert "[Speaker 2] second segment" in text


async def test_transcribe_falls_back_to_pywhispercpp(monkeypatch, tmp_path):
    """When mlx_whisper isn't importable, the next backend must run."""
    src = tmp_path / "podcast.mp3"
    src.write_bytes(b"\x00")

    monkeypatch.setattr(audio, "_chunk_audio", lambda path, chunk_seconds=1800: [path])
    monkeypatch.setattr(audio, "_is_apple_silicon", lambda: True)
    monkeypatch.setattr(audio, "_transcribe_with_mlx", lambda path, model: None)
    monkeypatch.setattr(
        audio,
        "_transcribe_with_pywhispercpp",
        lambda path, model: [{"text": "from pywhispercpp"}],
    )

    text = await audio.transcribe(src)
    assert "from pywhispercpp" in text
    assert "[Speaker]" in text


async def test_transcribe_returns_empty_when_no_backend(monkeypatch, tmp_path):
    """Graceful: no whisper backend importable → empty markdown, no crash."""
    src = tmp_path / "podcast.mp3"
    src.write_bytes(b"\x00")

    monkeypatch.setattr(audio, "_chunk_audio", lambda path, chunk_seconds=1800: [path])
    monkeypatch.setattr(audio, "_transcribe_with_mlx", lambda path, model: None)
    monkeypatch.setattr(audio, "_transcribe_with_pywhispercpp", lambda path, model: None)
    monkeypatch.setattr(audio, "_transcribe_with_openai_whisper", lambda path, model: None)

    text = await audio.transcribe(src)
    # Empty backend returns produce a placeholder line, but no real content.
    assert "## Chunk 1" in text
    assert "no transcript produced" in text


async def test_transcribe_returns_empty_for_missing_file():
    text = await audio.transcribe("/no/such/audio.mp3")
    assert text == ""


# ---------------------------------------------------------------------------
# URL path — temp file cleanup
# ---------------------------------------------------------------------------


async def test_transcribe_url_cleans_up_temp_file(monkeypatch):
    captured_paths: list[Path] = []

    async def _fake_fetch(url, *, timeout=120.0):
        return b"\x00\x01\x02"

    def _fake_chunk(path, chunk_seconds=1800):
        captured_paths.append(path)
        return [path]

    def _fake_transcribe_chunk(path, model):
        return ["[Speaker] hello"]

    monkeypatch.setattr(audio, "fetch_audio_bytes", _fake_fetch)
    monkeypatch.setattr(audio, "_chunk_audio", _fake_chunk)
    monkeypatch.setattr(audio, "_transcribe_chunk", _fake_transcribe_chunk)

    text = await audio.transcribe("https://example.com/cast.mp3")
    assert "[Speaker] hello" in text
    # The temp file the URL fetch created must be unlinked when transcribe exits.
    assert captured_paths
    assert not captured_paths[0].exists()


# ---------------------------------------------------------------------------
# transcribe_from_bytes / transcribe_sync
# ---------------------------------------------------------------------------


def test_transcribe_from_bytes_empty_input():
    assert audio.transcribe_from_bytes(b"") == ""


def test_transcribe_from_bytes_routes_through_pipeline(monkeypatch):
    monkeypatch.setattr(audio, "_chunk_audio", lambda path, chunk_seconds=1800: [path])
    monkeypatch.setattr(
        audio,
        "_transcribe_chunk",
        lambda path, model: ["[Speaker] from bytes"],
    )

    out = audio.transcribe_from_bytes(b"\x00\x01")
    assert "[Speaker] from bytes" in out
    assert "## Chunk 1" in out


def test_transcribe_sync_handles_local_path(monkeypatch, tmp_path):
    src = tmp_path / "local.wav"
    src.write_bytes(b"\x00")

    monkeypatch.setattr(audio, "_chunk_audio", lambda path, chunk_seconds=1800: [path])
    monkeypatch.setattr(
        audio,
        "_transcribe_chunk",
        lambda path, model: ["[Speaker] sync local"],
    )

    out = audio.transcribe_sync(str(src))
    assert "[Speaker] sync local" in out


# ---------------------------------------------------------------------------
# web_fetch dispatch
# ---------------------------------------------------------------------------


def test_is_audio_url_handles_query_string():
    assert web_fetch._is_audio_url("https://x.example/cast.mp3?token=abc") is True
    assert web_fetch._is_audio_url("https://x.example/cast.M4A") is True
    assert web_fetch._is_audio_url("https://x.example/index.html") is False


def test_is_audio_content_type_strips_charset():
    assert web_fetch._is_audio_content_type("audio/mpeg") is True
    assert web_fetch._is_audio_content_type("audio/mpeg; charset=binary") is True
    assert web_fetch._is_audio_content_type("text/html") is False
    assert web_fetch._is_audio_content_type(None) is False


async def test_web_fetch_routes_audio_url_to_transcribe(monkeypatch):
    """A ``.mp3`` URL must bypass trafilatura and land at audio.transcribe."""
    monkeypatch.setenv("RESEARCH_IGNORE_ROBOTS", "1")

    async def _no_archive(url, timeout: float = 30.0):
        return None

    monkeypatch.setattr(web_fetch.archive, "save", _no_archive)

    fetched: list[str] = []

    async def _fake_transcribe(url_or_path, *, model="base", chunk_seconds=1800, job=None):
        fetched.append(str(url_or_path))
        return "## Chunk 1 (00:00-30:00)\n\n[Speaker] routed-via-audio body"

    monkeypatch.setattr(audio, "transcribe", _fake_transcribe)

    async def _explode(*args, **kwargs):
        raise AssertionError("httpx should be skipped for audio URLs")

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _explode)

    source = await web_fetch.fetch("https://podcast.example/episode-42.mp3")
    assert source is not None
    assert source.source_kind == "audio"
    assert source.metadata["fetched_via"] == "audio"
    assert "[Speaker] routed-via-audio" in source.cleaned_text
    assert fetched == ["https://podcast.example/episode-42.mp3"]


async def test_web_fetch_routes_audio_content_type(monkeypatch):
    """Server-declared audio (no recognisable suffix) routes via transcribe_from_bytes."""
    monkeypatch.setenv("RESEARCH_IGNORE_ROBOTS", "1")

    async def _no_archive(url, timeout: float = 30.0):
        return None

    monkeypatch.setattr(web_fetch.archive, "save", _no_archive)

    fake_bytes = b"ID3fake-mp3-bytes"

    async def _fake_httpx(url, timeout, user_agent):
        return 200, "garbage", fake_bytes, "audio/mpeg"

    monkeypatch.setattr(web_fetch, "_fetch_via_httpx", _fake_httpx)

    consumed: list[bytes] = []

    def _fake_transcribe_from_bytes(data, *, suffix=".mp3", **kwargs):
        consumed.append(data)
        return "## Chunk 1 (00:00-30:00)\n\n[Speaker] from declared audio bytes"

    monkeypatch.setattr(audio, "transcribe_from_bytes", _fake_transcribe_from_bytes)

    source = await web_fetch.fetch("https://podcast.example/stream/episode")
    assert source is not None
    assert source.source_kind == "audio"
    assert source.metadata["fetched_via"] == "audio"
    assert source.metadata["status_code"] == 200
    assert consumed == [fake_bytes]


# ---------------------------------------------------------------------------
# Smoke registry wiring
# ---------------------------------------------------------------------------


def test_tool_registry_has_audio():
    from research_agent.tools import TOOL_REGISTRY

    assert "audio" in TOOL_REGISTRY
    assert callable(TOOL_REGISTRY["audio"])


def test_smoke_audio_summary_matches_contract(monkeypatch, tmp_path):
    """The smoke verb summary must include source / chunks / char_count / preview."""
    src = tmp_path / "talk.mp3"
    src.write_bytes(b"\x00")

    monkeypatch.setattr(audio, "_chunk_audio", lambda path, chunk_seconds=1800: [path])
    monkeypatch.setattr(
        audio,
        "_transcribe_chunk",
        lambda path, model: ["[Speaker] hello world"],
    )

    from research_agent.tools import _smoke_audio

    out = _smoke_audio(str(src))
    assert f"source: {src}" in out
    assert "chunks: 1" in out
    assert "char_count:" in out
    assert "preview:" in out
