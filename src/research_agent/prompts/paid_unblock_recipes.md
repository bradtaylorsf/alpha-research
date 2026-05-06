# Paid Resources That Would Unblock an Investigation — Recipe Catalog

This file is reference data, not a templated prompt. The orchestrator loads
it raw and injects it into the critic and synthesizer context payloads
under the `paid_unblock_recipes` key. The critic uses it to detect when a
specific *evidenced* gap maps to a known paid resource; the synthesizer
uses it to render the "Paid Resources That Would Unblock This
Investigation" section of the report.

## How to use this catalog

When the critic identifies a gap in coverage that a specific paid resource
would close, emit a `paid_opportunity` entry tied to the concrete
finding/subject. The synthesizer then groups those entries into **High
value** (the paid resource is the only realistic path to fill the gap or
the cheapest reliable one) and **Lower value** (cheaper public alternatives
may suffice; flag only because the gap is real and the operator should
make the call).

**Hard rules:**

- Only flag a paid resource when there is an **actual evidenced gap** in
  the report or findings — never recommend paid services to be thorough.
- Every entry must reference a specific finding, named subject, agency,
  or claim. Generic "you could subscribe to LinkedIn" is not allowed.
- Pull the service name and approximate cost range verbatim from this
  catalog. Do not invent prices or services.
- If no gap maps to a paid resource, omit the section entirely.

## Catalog

### Employment / professional networks of an individual
- **LinkedIn Premium / Sales Navigator** — $60–$150/mo — would clarify
  employment history, role transitions, mutual connections, and
  professional network of a named person beyond what public search
  surfaces.

### Aggregated people search (phone, address history, aliases)
- **Pipl** / **BeenVerified** / **Spokeo** — $30–$200/mo — would surface
  phone numbers, prior addresses, known aliases, and likely relatives for
  a named individual when public records and free search return little.

### Case law / state-court precedents beyond Casetext / CourtListener
- **Westlaw** / **Lexis Nexis** — $1k–$10k/yr (or pay-per-search) —
  would surface state-court precedents, secondary materials (treatises,
  ALR), and litigation history not indexed by free legal databases.

### Federal court docket pages not in RECAP
- **PACER pay-per-page** — $0.10/page (capped $3/document) — would pull
  the specific federal docket entry, motion, or exhibit when RECAP /
  CourtListener does not have it cached.

### State / county court records (TX, NY, FL, county clerks, etc.)
- **Per-jurisdiction pay-per-search portals** — varies — would surface
  state-court filings, civil judgments, liens, or criminal history at
  the county or state level when no aggregator covers that jurisdiction.

### Premium business news (paywalled)
- **WSJ** / **Bloomberg** / **Financial Times** subscriptions —
  $20–$50/mo each — would unlock paywalled investigative pieces,
  earnings coverage, or executive profiles that public previews tease.

### Trade press (regional construction / legal / industry journals)
- **ENR (Engineering News-Record)**, **Crain's** (Chicago / NY /
  Detroit), regional business journals — $200–$500/yr each — would
  surface industry-specific reporting on contracts, executives, project
  awards, and disputes that mainstream press does not cover.

### Property records aggregators (multi-county)
- **PropertyShark** / **ATTOM** — $50–$300/mo — would aggregate deed,
  mortgage, lien, and ownership-chain data across counties for a named
  property, owner, or LLC when single-county portals are insufficient.

### Glassdoor employee reviews at scale
- **Glassdoor for Employers** — $100+/mo — would surface salary bands,
  internal sentiment, and review counts on a named employer beyond the
  free five-review preview.

### Sanctions / OFAC commercial-grade with deduplication
- **Refinitiv World-Check** / **Dow Jones Risk & Compliance** — $$$$
  (enterprise pricing) — would deduplicate sanctions, PEP, and adverse-
  media hits across global lists when free OFAC search returns ambiguous
  matches for a common name.

### Real-time stock / financial data
- **Bloomberg Terminal** / **Refinitiv Eikon** — $$$$ ($24k+/yr) —
  would provide intraday pricing, holdings disclosures, and earnings
  transcripts when free EDGAR / public quotes are insufficient for the
  specific finding.

### OCCRP Aleph private-leak datasets
- **OCCRP Aleph** — free with verified-journalist credentials —
  would surface offshore leaks (Pandora / Paradise / Panama Papers),
  leaked corporate registries, and investigative datasets for a named
  individual or entity. Gated access; not paid in dollars but paid in
  verification.

## Notes for the critic and synthesizer

- The critic emits `paid_opportunity` entries with `service`,
  `cost_range`, `gap`, and `tier` (`high` or `low`). The synthesizer
  reads the rendered critique markdown (and structured fields exposed
  via `latest_critique`) and turns those entries into the report's
  "Paid Resources That Would Unblock This Investigation" section.
- Match by *evidenced* gap, not by domain. If the report does not
  actually rely on case-law precedent, do not flag Westlaw just because
  the topic is "legal".
- When two services compete (e.g., Pipl vs. BeenVerified), pick the one
  most commonly used in journalist workflows and note the alternative
  parenthetically.
- Operators can extend this catalog by editing this file directly; no
  code changes required.
