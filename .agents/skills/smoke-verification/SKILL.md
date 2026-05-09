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

## Behavioral acceptance criteria

When an AC says "re-run X and verify Y" (e.g. "Re-run the Project 2025 goal and verify ≥3 site-scoped queries emit" #178; "Re-run a goal with all subgoals closing → `research status` shows completed/goal_complete" #160; live re-run of Project 2025 and Cursor goals #118), green unit tests do NOT satisfy the AC. The live run must execute. If the run cannot execute in the current environment (no API key, no LLM access), the issue is PARTIAL — say so explicitly and do not claim full completion.

## Implementer responsibilities

- After running the smoke command, **read the output**. If empty or placeholder-laden, do not declare success — investigate.
- For Playwright connectors: live-verify selectors for *every distinct page type* (search results page AND profile/detail page). Reusing search-row selectors as profile-page selectors is a known footgun (#101).
- If the connector requires an optional binary (Tesseract, ffmpeg, mlx-whisper) or external service (LM Studio), and that dep is unavailable in the verification environment, the smoke must **skip loudly** with a clear marker — not silently emit empty output (#109).
- Test fixtures must contain detectable signal. An audio fixture that is silent or synthetic produces empty transcripts and gives a false-green (#110).

## Reviewer responsibilities

When the issue calls for live smoke verification, locate the smoke output in the implementation log and inspect it. Treat the following as failure regardless of exit code:

- Empty markdown body
- "No results" with no investigation note
- Structured fields rendering `?`, `None`, or empty for fields the AC names
- Default "Smoke command exited 0" success claim with no content shown

## Concrete pattern: non-empty content assertion in the smoke wrapper

A smoke wrapper that just prints results and exits 0 cannot fail on empty output. Assert content inside the wrapper:

```python
def _smoke_<connector>(query: str) -> None:
    results = search(query)
    if not results:
        # Loud failure — exit non-zero so the verifier sees the gap.
        print(f"[smoke FAIL] {connector_name}: search('{query}') returned 0 results", file=sys.stderr)
        sys.exit(1)
    rendered = render_markdown(results)
    if len(rendered.strip()) < MIN_EXPECTED_CHARS:
        print(f"[smoke FAIL] {connector_name}: output too short ({len(rendered)} chars)", file=sys.stderr)
        sys.exit(1)
    # Optional: spot-check a field the AC names.
    if "?" in rendered.split("\n")[0:5]:  # placeholder fields in header
        print(f"[smoke WARN] {connector_name}: placeholder fields detected — selectors may be stale", file=sys.stderr)
    print(rendered)
```

For optional-binary connectors, skip loudly:

```python
if not shutil.which("tesseract"):
    print("[smoke SKIP] tesseract not installed — OCR layer unverified", file=sys.stderr)
    sys.exit(0)  # 0 = skip-acceptable; 1 = real failure
```

## Other patterns that work

- For paid/keyed APIs, fail loudly when the key is missing (#114) rather than silently no-op.
- Use `uv tool install -e .` in `setup_command` so the verifier's `/bin/sh -c "research ..."` shell-out resolves on PATH; otherwise smoke fails with `command not found` (recurring across #100–#117).
- For Playwright connectors, write a debug screenshot on per-section selector miss (not just full-card miss) so DOM-drift debugging is fast (#94 LittleSis).
