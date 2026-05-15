---
name: sos
description: "Secretary of State business entity filings, entity status, registered agents, and corporate filing profiles."
when_to_use: "business entity lookup, registered agent, corporate status, LLC/corporation filings, Secretary of State business registry"
when_not_to_use: "election candidate rosters or ballot-qualified candidate lists → state_election_search; federal campaign filings → fec_search"
---

# Secretary of State business connector

Use `sos_search` for business entity registries: company name, entity number,
entity status, registered agent, and filing profile records.

Do not use this connector for election candidate rosters. State Secretaries of
State often operate both business and election divisions, but this connector is
wired to business filings. Use `state_election_search` for official election
candidate-list sources.

## Knobs available

- `state` — two-letter state code; CA is wired, other states may be stubs.
- `max_results` — default 25.

## Anti-patterns

- Do not issue `sos_search` for "all candidates", ballot status, or candidate
  filing lists.
- Do not treat a business registry miss as evidence that a candidate does not
  exist.
- Do not use this connector for campaign finance transactions; use FEC,
  CalAccess, or the state campaign-finance source instead.
