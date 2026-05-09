---
name: env-var-registration
description: New RESEARCH_* env vars must be registered in EXPECTED_ENV_KEYS, .env.example, and the README env table — same diff.
when-to-use: When introducing or modifying any environment variable read by `src/research_agent/`.
---

# Env Var Registration

This repo enforces a parity contract between three places. New `RESEARCH_*` env vars added at a call site without updating all three break `test_env_example_matches_expected_keys` and hide the flag from `research doctor`. Recurring violations: #108 (`RESEARCH_PDF_VLM_ESCALATION`), #109 (`RESEARCH_OCR_VLM_ESCALATION`), and the daemon-progress var.

## The three places (all required, same diff)

1. **`src/research_agent/config.py`** → add the key to `EXPECTED_ENV_KEYS`. This is the canonical surface that `research doctor` walks.
2. **`.env.example`** → add a commented-out line with a sane default and a one-line description.
3. **`README.md`** → add a row to the env table (variable, purpose, default).

If the var also gates a paid tier or external service, also update `docs/API_KEYS.md`.

## Naming

- Prefix: `RESEARCH_` for runtime/operator flags. Capability-named API keys may drop the prefix when they're standard third-party env names (e.g. `OPENAI_API_KEY`).
- Name by **capability**, not **broker** — `LINKEDIN_DATA_API_KEY` not `PROXYCURL_API_KEY` (#115). Brokers swap; capabilities are stable.

## Reading

Use `os.environ.get("NAME", default)` directly. Do not introduce a new config layer; `EXPECTED_ENV_KEYS` is the registry.

## Reviewer check

For every `os.environ.get("RESEARCH_*")` or new third-party API key in the diff:

```bash
grep -n "NAME" src/research_agent/config.py .env.example README.md docs/API_KEYS.md
```

All four (or three for non-API-key flags) must show a hit. If any is missing, add it before sign-off — the test will catch it but reviewer should not be the discovery path.
