# Recommended Human Follow-Ups — Recipe Catalog

This file is reference data, not a templated prompt. The synth orchestrator
loads it raw and injects it into the synthesizer's context payload under the
`followup_recipes` key. The synthesizer uses it to populate the
"Recommended Human Follow-Ups" section of every report — bridging the
categorical gaps where software cannot help and only humans can.

## How to use this catalog

When drafting the "Recommended Human Follow-Ups" section, match the
investigation's **domain** (securities fraud, public corruption, healthcare,
etc.) to the relevant block(s) below and pull the specific hotline, agency,
form, or statute by name. Every recommendation must end with a one-line
`because <reason>` tying it to a concrete finding or named subject from the
report — never recommend a hotline generically. If a domain doesn't apply,
omit it; do not pad the section.

## Generic fact-check / FOIA prompts (apply to almost every report)

- **Any named subject (person or org)** → identify their spokesperson,
  press contact, or opposing counsel of record and recommend calling for
  on-the-record response to the strongest specific claim against them.
- **Any licensed entity** (contractor, broker, attorney, physician, etc.)
  → check the relevant state licensing board's public discipline record,
  and recommend a FOIA / public-records request for the full disciplinary
  file (board complaints, investigator notes, settlement agreements).
- **Any government action cited** (permit, contract award, enforcement
  letter) → recommend a FOIA / state public-records request for the
  underlying file (correspondence, internal memos, scoring sheets).
- **Any sealed or redacted court filing referenced** → flag as a
  motion-to-unseal candidate; identify the case number and court.
- **Any claim that names a specific person's conduct** → flag for
  pre-publication libel review; defamation risk scales with specificity.

## Domain-specific recipes

### Securities fraud / market manipulation / investor harm
- **SEC Tip, Complaint, and Referral (TCR) form** — `sec.gov/tcr` —
  whistleblower awards possible under Dodd-Frank §922.
- **FINRA Whistleblower / Office of the Whistleblower** —
  `finra.org/rules-guidance/key-topics/whistleblower` — for
  broker-dealer misconduct.
- **State securities regulator (NASAA member)** — for state-registered
  advisers and intrastate offerings.

### Federal corruption / abuse of office / contractor fraud (federal)
- **DOJ Office of Inspector General Hotline** — `oig.justice.gov/hotline`
  — for misconduct by DOJ employees and federal officials.
- **FBI Public Corruption Unit** — local FBI field-office tip line or
  `tips.fbi.gov` — for bribery, kickbacks, abuse of office.
- **GAO FraudNET** — `gao.gov/about/what-gao-does/fraudnet` — for waste,
  fraud, abuse in federal programs.
- **Agency-specific OIG** (e.g., Pentagon IG, USPS IG, EPA OIG) — when the
  alleged misconduct is inside a specific federal agency.

### State / local corruption
- **State Attorney General public-corruption / consumer-protection
  hotline** — most states publish a tip line; cite by state.
- **State ethics commission** — for officeholder financial-disclosure
  violations and conflicts of interest.
- **Local district attorney's public-integrity unit** — for county- and
  city-level officials.

### Healthcare fraud / Medicare-Medicaid abuse / patient safety
- **HHS-OIG Hotline** — `oig.hhs.gov/fraud/report-fraud` —
  Medicare/Medicaid fraud, kickbacks, patient harm.
- **CMS Medicare fraud line** — `1-800-MEDICARE` — beneficiary-reported
  billing fraud.
- **State Medicaid Fraud Control Unit (MFCU)** — for state-program
  Medicaid fraud and nursing-home abuse.
- **State medical / nursing licensing board** — for clinician-specific
  misconduct; FOIA the full complaint file.

### Tax fraud / abuse of nonprofit status
- **IRS Whistleblower Office (Form 211)** — `irs.gov/whistleblower` —
  awards tied to recovered tax owed.
- **State charity registry / AG charities bureau** — for 501(c)(3)
  governance, self-dealing, or solicitation fraud.
- **IRS Form 13909** — public complaint about a tax-exempt org.

### Financial elder abuse / vulnerable-adult exploitation
- **State Adult Protective Services (APS)** — every state has one;
  mandatory-reporter pathways apply.
- **FINRA Securities Helpline for Seniors** — `1-844-57-HELPS` — for
  investment exploitation of seniors.
- **Local law-enforcement elder-abuse unit** — many DAs have one.

### OSHA / workplace safety / labor whistleblower
- **OSHA Whistleblower Protection Program** — `whistleblowers.gov` —
  retaliation complaints under 20+ statutes.
- **NLRB** — for unfair labor practices and protected concerted activity.
- **State labor commissioner** — for state-law wage-theft and retaliation.

### Environmental / EPA
- **EPA Tipline / "Report Environmental Violations"** —
  `epa.gov/enforcement/report-environmental-violations`.
- **State environmental agency hotline** (e.g., CalEPA, NYSDEC) — for
  state-permitted facilities.
- **National Response Center** — `1-800-424-8802` — for chemical /
  oil-spill emergencies.

### Defense / federal contractor / classified misconduct
- **DoD Hotline** — `dodig.mil/Hotline` — fraud, waste, abuse by DoD
  personnel and contractors.
- **Intelligence Community IG** — for IC-specific concerns; use proper
  channels for classified material.
- **DCAA / DCMA** — contract-audit avenues for cost-mischarging.

### Immigration / ICE / CBP misconduct
- **DHS OIG Hotline** — `oig.dhs.gov/hotline`.
- **CBP Office of Professional Responsibility** — `1-877-2INTAKE`.
- **Civil Rights and Civil Liberties (CRCL) complaints** — DHS-wide.

### Banking / consumer financial / housing
- **CFPB consumer complaint** — `consumerfinance.gov/complaint`.
- **OCC Customer Assistance** — for nationally chartered banks.
- **FTC Consumer Sentinel** — for deceptive-practices intake.
- **HUD Office of Fair Housing** — for housing-discrimination complaints.

## Notes for the synthesizer

- Prefer the most specific channel available. "Federal corruption" → name
  the agency's IG, not just "DOJ".
- When two hotlines compete (e.g., HHS-OIG vs. state MFCU), recommend
  both with a one-line distinction (federal vs. state program).
- For FOIA candidates, name the **specific record** ("disciplinary file
  for license #12345"), the **agency**, and the **statute** (FOIA, or the
  state's equivalent — e.g., California Public Records Act).
- Operators can extend this catalog by editing this file directly; no
  code changes required.
