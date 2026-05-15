---
name: state_election
description: "Official state election office candidate roster lists and portals for ballot-qualified or candidate-listed rows."
when_to_use: "candidate rosters, ballot-qualified status, state election office candidate lists, official candidate filing lists"
when_not_to_use: "business entities → sos_search; federal FEC-filed status → fec_search kind=candidates_enumerate; campaign finance transactions → calaccess/fec"
---

# State election connector

Use `state_election_search` when the user needs official state election office
candidate rows: candidate name, party, office/chamber, district/seat, and
ballot or filing status.

This connector is distinct from `sos_search`. In this codebase,
`sos_search` means Secretary-of-State **business entity** filings. It is not
a candidate roster source.

## Knobs available

- `state` — required two-letter jurisdiction code.
- `office` — optional office/chamber filter such as `House`, `Senate`, or a
  contest name.
- `cycle` — optional election year; used to warn when the recipe does not
  declare coverage for that cycle.
- `max_results` — default 50.

## Anti-patterns

- Do not use `sos_search` for candidate rosters; it searches business filings.
- Do not treat state ballot-qualified status as FEC-filed status. Use
  `fec_search` with `kind=candidates_enumerate` for federal filing rosters,
  then use this connector for official state ballot/candidate-list status.
- Do not accept a portal description as a roster. The task is useful only
  when it returns candidate-shaped rows.

## Recipe coverage

Initial recipes cover CA, TX, FL, NY, PA, GA, SC, IL, AZ, NC, CO, OK, and MD.
Each recipe records source URL, source type, office and cycle coverage,
retrieval method, and known limitations in `config/state_election_recipes.yaml`.

## Output shape

Each `SearchResult` has `source_kind="state_election"` and `extras` with:
`state`, `chamber`, `district_or_seat`, `candidate_name`, `party`, `status`,
`source_url`, `source_type`, `retrieval_timestamp`, and `confidence`.
