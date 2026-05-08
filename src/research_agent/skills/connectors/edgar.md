---
name: edgar
description: "SEC EDGAR full-text search across public-company filings. Without form_type the planner mixes annual reports with insider trades; missing RESEARCH_USER_AGENT email fails loudly."
when_to_use: "public-company filings, annual / quarterly reports, insider trading (Form 4), proxy statements, 8-K material events, IPO prospectuses, executive comp"
when_not_to_use: "private companies → opencorporates; nonprofits → nonprofits (990s); state corp records → sos; campaign finance → fec"
---

# EDGAR connector

SEC EDGAR full-text search. Returns up to ~10 years of filings; HTML/XML extraction happens in `fetch()`, not search.

## Form-type routing (CRITICAL)

Without `form_type`, EDGAR returns every filing kind interleaved. Pick the form that matches the question.

| form_type | What it is | When to ask for it |
|---|---|---|
| `10-K` | Annual report | Yearly financials, risk factors, segment breakdowns |
| `10-Q` | Quarterly report | Most recent quarter financials |
| `8-K` | Current report (material event) | Cybersecurity incidents, exec departures, acquisitions, restatements |
| `Form 4` | Insider transaction | Officer/director buys/sells; pass `["4"]` |
| `S-1` | IPO registration | Pre-IPO disclosures |
| `DEF 14A` | Definitive proxy | Executive comp, board nominees, shareholder proposals |
| `13F-HR` | Institutional holdings | Quarterly long positions for funds with >$100M AUM |

`form_type` accepts a string (`"10-K"`) or list (`["10-K", "10-Q"]`).

## Query construction

- Use the company's common name or ticker. EDGAR's FTS resolves both. Add a topic keyword for 8-Ks (`"Acme Corp cybersecurity"`).
- For Form 4 insider lookups, query the issuer name and pass `form_type="4"`; the filer is the insider, the issuer is what you're searching.
- CIK lookup: if you already know the CIK (10-digit zero-padded), filter post-hoc on `extras.cik` rather than encoding it in the query — EDGAR FTS is keyword-based, not identifier-routed.

## Knobs available

- `form_type` — string, list, or tuple of form codes (e.g. `"10-K"` or `["10-K", "10-Q"]`). Optional; omit for all forms.
- `max_results` — default 20.
- `timeout` — seconds; default 15.

## Anti-patterns

- ❌ Searching `"Acme Corp"` without `form_type` and expecting only 10-Ks — you'll get every form interleaved.
- ❌ Running search() without setting `RESEARCH_USER_AGENT` to a contact email — SEC's published policy requires it; this connector fails loudly with a clear message instead of silently 403'ing.
- ❌ Treating a search hit's `snippet` as the filing's content — snippets are the EDGAR FTS preview. Use `fetch()` on the accession URL for the actual document.
- ❌ Using `form_type="10K"` (no dash) — SEC encodes it as `10-K`.

## When to fan out

Search returns the EDGAR index page or filing-detail URL keyed by accession number. To extract financials / risk factors / 8-K item text, emit a follow-up `fetch()` per accession — `fetch()` resolves the index, picks the primary doc (HTML 10-K, XML Form 4), and runs `trafilatura` for narrative extraction.

## Output shape

Each `SearchResult` carries `url` (accession-keyed), `title` (form + company), `snippet` (FTS preview), `published_at` (file date), `source_kind="sec"`, and `extras` with `cik`, `accession`, `form`, `company`, `file_type`. The actual filing body comes from `fetch()`.

## Auth

`RESEARCH_USER_AGENT` must be set to `"Your Name your@email"` per SEC policy. The connector enforces an `@` and fails loudly if missing.
