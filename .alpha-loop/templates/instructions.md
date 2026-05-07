<!-- managed by alpha-loop -->
Updated `AGENTS.md` to match the current state of the repo. Key changes:

- Dropped the "(to be created)" framing — `src/research_agent/` is real now, with all subpackages enumerated by their actual filenames.
- Added the new bits: `prompts/` markdown templates (with a code-style rule to edit those instead of inlining strings), `storage/disk_cap.py` cap, `data/diagnostics/`, `docs/API_KEYS.md`, `scripts/test.sh`, `models.local.yaml` overlay, `report.history` and `daemon.{out,err}.log` in the per-job folder contract.
- Refreshed the Tech Stack source list to reflect the actual connectors (Playwright + httpx/trafilatura/readability + arxiv/feedparser + waybackpy + pypdf/unstructured + `gh`), removing the Tavily/Brave/Exa/PRAW mentions that contradicted the no-paid-APIs rule.
- Noted that the repo is now public (LICENSE/CONTRIBUTING/SECURITY shipped), and reinforced "treat every diff as if strangers will read it" in the secrets clause.
- Preserved the 5-section structure, the alpha-loop marker, and every existing project-specific rule.
