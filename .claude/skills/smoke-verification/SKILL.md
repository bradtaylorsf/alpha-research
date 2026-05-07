---
name: smoke-verification
description: Smoke and verification commands must assert non-empty, query-relevant content — exit code 0 alone is not success.
when-to-use: When implementing or reviewing any tool/connector that ships a `_smoke-tool` verb or live verification command.
---

# Smoke Verification

Across issues #102 (GovInfo), #104 (USAspending), #109 (OCR), #110 (audio), #101 (CA SoS), #95 (BBB), and #94 (LittleSis), this loop has shipped "successful" connectors whose smoke commands returned empty markdown, zero result rows, or `?` placeholder fields — yet exit code was 0 and the issue was closed green.

**Empty output is a failure signal, not a pass.**

## Rule

A smoke / verification command "passes" only when:

1. Exit code is 0, AND
2. Output is non-empty, AND
3. Output contains query-relevant content (entity name, value, ID, or expected field), AND
4. No required structured fields are placeholder (`?`, `null`, empty string).

## Implementer responsibilities

- After running the smoke command, read its output. If empty or placeholder-laden, *do not* declare success — investigate.
- For Playwright connectors: live-verify selectors for *every distinct page type* (search results page AND profile/detail page). Reusing search-row selectors as profile-page selectors is a known footgun (#101).
- If the connector requires an optional binary (Tesseract, ffmpeg, mlx-whisper) or external service (LM Studio), and that dep is unavailable in the verification environment, the smoke must **skip loudly** with a clear marker — not silently emit empty output (#109).
- Test fixtures must contain detectable signal. An audio fixture that is silent or synthetic produces empty transcripts and gives a false-green (#110).

## Reviewer responsibilities

When the issue calls for live smoke verification, locate the smoke output in the implementation log and inspect it. Treat the following as failure regardless of exit code:

- Empty markdown body
- "No results" with no investigation note
- Structured fields rendering `?`, `None`, or empty for fields the AC names
- Default "Smoke command exited 0" success claim with no content shown

## Patterns that work

- Add a non-empty content assertion to the smoke wrapper itself (e.g. `assert "|" in output and len(output) > 200`).
- For paid/keyed APIs, fail loudly when the key is missing (#114) rather than silently no-op.
- Use `uv tool install -e .` in `setup_command` so the verifier's `/bin/sh -c "research ..."` shell-out resolves on PATH; otherwise smoke fails with `command not found` (recurring across #100–#117).
