---
version: "1"
model_tier: general
description: Alternative source recipes for repeatedly low-yield direct connectors.
---
followups:
  - kind: calaccess_search
    if_zero_for_query_family: "city/county-level candidate"
    suggest: "FPPC/CalAccess primarily indexes state-level candidates and committees. For city/county-level campaign finance, try the city clerk or county elections portal for Form 460 records."
  - kind: courtlistener_search
    if_zero_for_query_family: "state-court or missing RECAP coverage"
    suggest: "CourtListener/RECAP may not include state-court records or unmirrored PACER filings. Try the local court eFile portal, docket search, or clerk records request."
  - kind: opencorporates_search
    if_zero_for_query_family: "new or unfunded entity"
    suggest: "OpenCorporates can lag or omit newer/unfunded entities. Try the state Secretary of State business search directly."
  - kind: edgar_search
    if_zero_for_query_family: "private company or non-issuer"
    suggest: "EDGAR covers SEC registrants and filings. For private companies, try state SoS records, litigation dockets, local permits, or news archives."
  - kind: congress_search
    if_zero_for_query_family: "older congress or non-federal action"
    suggest: "Congress.gov coverage can vary for older material and does not cover state/local action. Try GovInfo, committee archives, state legislature portals, or local government agendas."
  - kind: fec_search
    if_zero_for_query_family: "state or local campaign finance"
    suggest: "FEC covers federal campaign finance. For state or local races, try the state campaign-finance portal, county registrar, or city clerk filings."
  - kind: fec_search
    if_zero_for_query_family: "federal candidate roster enumeration"
    suggest: "For complete federal candidate rosters, use fec_search with kind=candidates_enumerate and structured cycle/office/state/district filters; if that remains empty, confirm whether the filing window has opened or use state ballot-qualified rosters."
