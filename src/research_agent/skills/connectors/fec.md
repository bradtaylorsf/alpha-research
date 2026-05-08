---
name: fec
description: "OpenFEC — federal candidates, committees, individual contributions, independent expenditures. Without cycle/year keywords in the query, results span every election since the 1980s."
when_to_use: "federal campaign finance, candidate FEC filings, PAC / super-PAC committees, individual donor lookup, independent expenditures for or against a candidate"
when_not_to_use: "state-level campaign finance → calaccess (CA) or sos; lobbying spend → lda; corporate political giving via 501(c)(4)s → nonprofits; FEC enforcement complaints → courtlistener"
---

# FEC connector

Searches the OpenFEC REST API across four indices. Default `kind=candidates`.

## Index selection

| `kind` | What it is | Query target |
|---|---|---|
| `candidates` | Federal candidates (House / Senate / President) | Candidate name |
| `committees` | PACs, super PACs, party committees, principal campaign committees | Committee name |
| `schedules/schedule_a` | Individual contributions to committees | Contributor name |
| `schedules/schedule_e` | Independent expenditures (for/against a candidate) | Payee name (vendor/firm spending the money) |

For "who funded X?", route to `schedules/schedule_a` with the candidate's principal committee name in the query, not the candidate name. For "who spent against Y?", use `schedules/schedule_e`.

## Cycle / year filtering (CRITICAL)

The connector does **not** currently expose a `cycle` knob. Include the cycle year in the query string to scope to a specific election.

| Office | Cycles |
|---|---|
| Presidential | 2024, 2028, 2032 (every 4 yrs) |
| Senate | 2024, 2026, 2028 (every 2 yrs, 1/3 of seats) |
| House | 2024, 2026, 2028 (every 2 yrs, all seats) |

Without "2024" or "2026" in the query, you get hits across every cycle a candidate ever ran in.

## Candidate / committee ID format

OpenFEC IDs follow a strict prefix pattern:

| Prefix | Type | Example |
|---|---|---|
| `P0…` | Presidential candidate | `P00009423` (Trump 2024) |
| `H0…` / `H4…` / `H8…` | House candidate | `H4CA12345` |
| `S0…` / `S4…` / `S8…` | Senate candidate | `S6OH00163` |
| `C0…` | Committee (any kind) | `C00580100` |

Resolve a candidate ID via `kind=candidates`, then pass the principal committee ID (a `C0…` from the candidate's `extras`) to the schedule searches — schedule queries route by committee, not candidate.

## Query construction

- For donor lookups, use the contributor's full name as it appears on the disclosure (`"Smith, John"` order is common but the API tolerates both). Add the employer or city when the name is common.
- For committee lookups, the formal committee name beats the abbreviation (`"Trump Make America Great Again Committee"` over `"MAGA"`).
- Cycle keyword: append the year (`"2024"`) to constrain.

## Knobs available

- `kind` — `candidates` (default), `committees`, `schedules/schedule_a`, or `schedules/schedule_e`.
- `max_results` — default 20.
- `timeout` — seconds; default 15.

## Anti-patterns

- ❌ `search("Donald Trump", kind="schedules/schedule_a")` — schedule_a queries the *contributor* name. Use `kind=candidates` first to resolve the principal committee ID, then pivot.
- ❌ Searching with no cycle keyword — surfaces every cycle the person/committee ran in; modern cycles drown.
- ❌ Treating an `H0…` ID as a committee ID — H/S/P prefixes are *candidate* IDs; committee IDs start with `C0`.
- ❌ Using `kind="contributions"` — not a valid kind. Use `schedules/schedule_a`.

## When to fan out

Candidate / committee results carry the FEC.gov permalink and the full set of IDs in `extras`. Schedule results have the transaction-level row inline (amount, date, contributor employer/occupation). Fan out to:
- the principal-committee schedule_a search to enumerate donors,
- the schedule_e search keyed on a target candidate to find independent expenditures.

## Output shape

Each `SearchResult` carries `url` (fec.gov), `title`, `snippet`, `published_at` (filing date / contribution date), `source_kind="fec"`, and `extras` with `candidate_id`, `committee_id`, `cycle`, `office`, `party`, `state`, `district` (where applicable). For schedule_a, `extras` adds `contributor_name`, `contributor_employer`, `contributor_occupation`, `amount`.

## Auth

Shared `DATA_GOV_API_KEY` (also used by `congress`). Loud failure if unset.
