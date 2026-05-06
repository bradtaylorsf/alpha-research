# Contributing to alpha-research

Thanks for your interest. This is an actively-developed research agent —
contributions, bug reports, and connector PRs are all welcome.

## How work is organized here

Every change starts as a GitHub issue. The repo is driven by an
issue → branch → PR → main loop, and the open issues list IS the
roadmap (see [#107](../../issues/107) for the connector buildout epic).

If you're not sure where to start, look for issues labeled
[`good first issue`](../../labels/good%20first%20issue) or
[`help wanted`](../../labels/help%20wanted).

## Setting up

```bash
git clone https://github.com/bradtaylorsf/alpha-research.git
cd alpha-research
pip install -e ".[dev]"
playwright install chromium
cp .env.example .env  # then add your keys
uv run research doctor
```

`research doctor` will tell you what's missing. The minimum to run
locally is **LM Studio** (with the gemma models loaded — see
`config/models.local.yaml`) plus the `--local` flag at runtime. Cloud
mode needs `OPENROUTER_API_KEY`. Brave Search requires
`BRAVE_SEARCH_API_KEY` (free tier is plenty).

## Workflow

- **Branch naming:** `issue-<N>-<slug>` (e.g. `issue-93-courtlistener`).
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/) — `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`.
  Reference the issue (e.g. `feat: courtlistener connector (#93)`).
- **Tests:** every new module ships with tests. Connector modules
  follow the existing pattern (mock httpx for API connectors, mock
  Playwright for browser connectors — see `tests/test_tools_reddit.py`
  for the JSON-API style and `tests/test_tools_news.py` for RSS).
- **PRs:** target `main`. Use `Closes #N` in the description to
  auto-close the issue.

## Connector contract

If you're adding a new connector under `tools/`, it must implement:

```python
async def search(query: str, **kwargs) -> list[SearchResult]: ...
async def fetch(url: str) -> Source | None: ...
```

The `SearchResult` and `Source` Pydantic models live in
`tools/models.py`. Register the connector in `tools/__init__.py` so
the smoke verb (`research _smoke-tool <name> "<query>"`) can drive it.
Update `prompts/planner.md` to teach the planner when to emit the new
task kind.

See `tools/reddit.py` (JSON API) and `tools/news.py` (RSS + scrape
fallback) for the canonical patterns.

## Testing

```bash
uv run pytest -q        # full sweep, ~700 tests
uv run pytest tests/test_tools_<your-connector>.py -v
```

Unit tests should never hit the live network or the live LLM. Use
`monkeypatch` to inject httpx / Playwright stubs.

## Reporting bugs

Open an issue with:
- What you ran (`research start --local --goal "..."`)
- What happened (relevant lines from `jobs/<id>/events.jsonl`)
- What you expected
- Doctor output (`uv run research doctor --json`)

## License

MIT — see [LICENSE](LICENSE).
