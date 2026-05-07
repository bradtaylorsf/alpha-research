# API Keys & Signup Reference

Every connector that needs an API key, where to get the key, and which
environment variable to set. Add what you need to `.env` (which is
gitignored — never commit keys).

The agent runs **without** any of these keys — local-mode plus DDG
Playwright fallback works at $0. Each key just *upgrades* a specific
connector. Sign up only for the connectors you actually plan to use.

---

## Already configured (you should have these)

| Key | Required? | Where to get it | Cost |
|---|---|---|---|
| `OPENROUTER_API_KEY` | yes (cloud mode only) | <https://openrouter.ai/settings/keys> | pay-per-token |
| `BRAVE_SEARCH_API_KEY` | optional but recommended | <https://api.search.brave.com/app/keys> | **free** 2K/mo |

If you only run with `--local` (LM Studio, gemma), even OpenRouter is
optional — LM Studio is your inference backend and there's nothing to
authenticate.

---

## Free public APIs (one-time signup, no cost)

These all use the same `api.data.gov` umbrella key — sign up **once**,
use it across multiple connectors.

| Connector | Issue | Env var | Where to get it | Rate limit (authenticated) |
|---|---|---|---|---|
| FEC OpenFEC | #94 | `DATA_GOV_API_KEY` | <https://api.data.gov/signup/> | 1,000 req/hr |
| Congress.gov | #99 | `DATA_GOV_API_KEY` | (same key as above) | 5,000 req/hr |
| Regulations.gov | (future) | `DATA_GOV_API_KEY` | (same key) | varies |

Connector-specific free keys (separate signups):

| Connector | Issue | Env var | Where to get it | Notes |
|---|---|---|---|---|
| CourtListener / RECAP | #93 | `COURTLISTENER_API_TOKEN` | <https://www.courtlistener.com/sign-in/> → Profile → API | Free with email signup; 5,000 req/hr authenticated |
| Senate LDA | #103 | `LDA_API_KEY` (optional — anonymous works at lower rate) | <https://lda.senate.gov/api/register/> | Anonymous tier sufficient for most use; key just raises the rate |
| YouTube Data API v3 (search) | #111 | `YOUTUBE_API_KEY` | <https://console.cloud.google.com/apis/credentials> → enable "YouTube Data API v3" | Free tier: 10,000 quota units/day |
| OpenCorporates | #92 | `OPENCORPORATES_API_KEY` | <https://opencorporates.com/info/about> (request public-benefit access) | Anonymous v0.4 access is gated (HTTP 401 as of 2026-05); a key is required for any live request. Without one, the connector returns no results and smoke skips cleanly. Commercial pricing £2,250–£12,000/yr |

---

## No-key connectors (work anonymously)

These connectors are free *and* require no signup. Listed here so you
know the env-var landscape is complete:

| Connector | Issue | Notes |
|---|---|---|
| SEC EDGAR | #98 | Required: `RESEARCH_USER_AGENT` must include a contact email (e.g. `research-agent you@example.com`) — SEC enforces this server-side; smoke gracefully skips when unset |
| ProPublica Nonprofit Explorer | #100 | Anonymous |
| Federal Register | #102 | Anonymous |
| USAspending.gov | #104 | Anonymous |
| GDELT 2.0 | #105 | Anonymous |
| OFAC sanctions | #116 | Treasury bulk download; anonymous |
| LittleSis | #97 | Anonymous |
| BBB profile lookup | #95 | Playwright; no API |
| State Secretary of State | #101 | Playwright; no API |
| State licensing boards (CSLB) | #91 | Playwright; no API |
| Cal-Access / Power Search | #96 | Playwright; no API |
| archive.today fallback | #106 | Anonymous |

---

## Paid (operator-controlled, optional)

These connectors only fire when you explicitly configure their key.
The agent uses them only for specific high-leverage gaps where free
sources have been exhausted (and the synth/critique pass flags this
explicitly per #113).

| Connector | Issue | Env var | Where to get it | Approx cost |
|---|---|---|---|---|
| Google Scholar via SERPAPI | #114 | `SERPAPI_KEY` | <https://serpapi.com/users/sign_up> | $75/mo for 5K queries (Scholar is one engine of many they offer) |
| LinkedIn via Proxycurl (default broker) | #115 | `LINKEDIN_DATA_API_KEY` | <https://nubela.co/proxycurl/> → Sign up → API Key | $0.01–$0.05 per profile lookup |
| LinkedIn via Lix (alternate broker) | #115 | `LIX_API_KEY` (set `LINKEDIN_BROKER=lix` to switch) | <https://lix-it.com/> → Sign up | Similar per-lookup pricing to Proxycurl |

For LinkedIn the connector is **broker-pluggable**: `LINKEDIN_BROKER`
selects the recipe (`proxycurl` by default, or `lix`). Each broker
reads its own key — see the rows above. Adding another broker is a
recipe-layer change in `tools/linkedin.py`. Use whichever broker your
wallet and TOS comfort allow.

---

## "Eventually" — paid but worth budgeting if you go deep

Not connectors yet, but worth knowing about for specific investigations:

| What | Approx cost | When to spend |
|---|---|---|
| LinkedIn Premium | $60/mo | Manual people-research without a broker |
| Pipl / BeenVerified / Spokeo | $30–$200/mo | Aggregated people-search (phone, address history, aliases) |
| Westlaw / LexisNexis | $1k–$10k/yr | Comprehensive case law beyond CourtListener |
| PACER (federal court fetches not in RECAP cache) | $0.10/page (cap $3/doc) | Sealed-but-recent federal filings |
| WSJ / Bloomberg / FT | $20–$50/mo each | Premium news with paywall scoops |
| Trade press (ENR, Crain's regional) | $200–$500/yr | Industry-specific reporting |

---

## What to do with this list

1. **Decide which categories matter to you.** YouTube channel research?
   You probably want the YouTube key. Political ambient? `DATA_GOV_API_KEY`
   + `COURTLISTENER_API_TOKEN`. Documentary research? `LDA_API_KEY` and
   the YouTube key.

2. **Sign up over a week or two as you build.** No need to do all at
   once. Each connector ships independently; you can enable them as
   issues land.

3. **Add keys to `.env` as you go.** `cp .env.example .env`, add the keys
   you've collected, restart any open shell. Then `uv run research doctor`
   confirms what's set.

4. **Track which connectors are "live" vs "skipped".** A connector with
   a key set runs; one without prints "would need `<ENV_VAR>`; live test
   skipped" and exits 0. The post-epic verification surfaces all the
   skipped ones in one place.

Note: not everything the agent depends on is an API key. `tesseract` is
a system prerequisite (not an env var) required for the PDF OCR
escalation layer — install it via `brew install tesseract` (macOS) or
`apt install tesseract-ocr` (Debian/Ubuntu). `research doctor` surfaces
its absence as a `skip` with an install hint.

## Privacy note

Keys live in `.env` and `.env.local`, both of which are gitignored. If
you accidentally commit a key (this happens to everyone eventually):

1. Rotate the key at the provider immediately.
2. Force-pushing the secret out of git history is hard and incomplete
   (caches exist). Rotation is the only real fix.
