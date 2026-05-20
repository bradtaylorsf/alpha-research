# Job Folder Contract

This document defines the read contract for `jobs/<job-id>/`. External
consumers such as MCP servers, Python embedders, and future UIs should read
from this folder through `research_agent.contract` and must not infer state
from daemon internals.

`job.json` has `schema_version: 2`. Any incompatible change to stable files
below requires bumping that version in the same PR.

Schema version history:

- `2` — issue #358: `plan/NNNN.json` subgoals now carry an integer
  `stage` field (default `1`) so the dossier stage ladder can detect
  premature subgoal closure. Plans written under schema 1 deserialise
  unchanged (every subgoal becomes `stage=1`).
- `1` — initial stable contract.

## Stability

Stable public files:

- `job.json`
- `intake.json`
- `goal.md`
- `plan/NNNN.{md,json}`
- `findings/NNNNNN.{md,json}`
- `sources/<sha256>.{md,json}`
- `synthesis/NNNN.{md,json}`
- `critique/NNNN.{md,json}`
- `report.md`
- `report.history/*.md`
- `events.jsonl`

Internal or transient files:

- `daemon.pid`, `daemon.out.log`, `daemon.err.log`
- `STOP`
- `RESUME_REPLAN.json`, `INBOX_REPLAN.json`
- `archive/report-*.md`
- `fragments/<section>/NNNN.{md,json}`
- `critique/fragments/<section>/NNNN.{md,json}`
- `coverage.json`
- `inbox/`, `inbox/processed/`
- `artifacts/`
- `*.tmp`, `*.partial.md`, and other incomplete writer artifacts

Internal paths may change without a schema bump. Stable paths may gain
optional fields, but removing or changing the meaning of an existing field
requires a `job.json` schema-version bump.

## Layout

```text
jobs/<job-id>/
├── job.json
├── intake.json
├── goal.md
├── plan/
│   ├── 0001.md
│   └── 0001.json
├── findings/
│   ├── 000001.md
│   └── 000001.json
├── sources/
│   ├── <sha256>.md
│   └── <sha256>.json
├── synthesis/
│   ├── 0001.md
│   └── 0001.json
├── critique/
│   ├── 0001.md
│   └── 0001.json
├── report.md
├── report.history/
└── events.jsonl
```

## File Shapes

### `job.json`

Canonical disk metadata for one job.

```json
{
  "schema_version": 2,
  "id": "2026-05-16-investigate-widget-co",
  "goal": "Investigate Widget Co",
  "domain": "general",
  "status": "pending",
  "created_at": 1778956800,
  "last_activity_at": 1778956800,
  "completion_reason": "goal_complete",
  "intake": {}
}
```

Fields:

- `schema_version` integer, current stable contract version.
- `id` deterministic `YYYY-MM-DD-<slug>` folder name.
- `goal` original research goal.
- `domain` optional intake domain.
- `status` lifecycle state such as `pending`, `running`, `completed`,
  `stopped`, or `failed`.
- `created_at` Unix seconds.
- `last_activity_at` optional Unix seconds, updated on status changes.
- `completion_reason` optional terminal reason such as `goal_complete`,
  `time_cap`, `budget_cap`, `task_cap`, `user_stopped`, `exhausted`, or
  `confirmed_gap`.
- `intake` frozen intake object used to start the run.

### `intake.json`

Frozen operator inputs and runtime caps. Known fields include:

- `goal` string.
- `domain` string or null.
- `time_cap_hours` number or null.
- `budget_cap_usd` number or null.
- `disk_cap_gb` number.
- `translate_non_english` boolean.
- `fragments` boolean.
- `inbox` boolean.
- `corpus` optional local corpus path.
- `max_tasks` optional integer task cap.
- `local` optional boolean for all-local model routing.
- `enrichment` optional object for CSV artifact enrichment.

Additional intake keys are allowed when introduced by a documented CLI flag.

### `goal.md`

Plain UTF-8 markdown containing the human-readable goal text. It is a direct
operator-facing mirror of `intake.goal`.

### `plan/NNNN.md`

Human-readable planner version. Shape:

- H1 `# Plan vNNNN`.
- `Created: <ISO-8601 UTC datetime>`.
- Fenced JSON block containing the planner payload.

### `plan/NNNN.json`

Raw planner payload. Current top-level fields are planner-defined and may
include `tasks`, `subgoals`, `scope_class`, `active_strategies`, and
`cornerstone_url`. Consumers should treat the JSON sidecar as structured
planner state and the markdown as display text.

Each entry in `subgoals` carries (schema 2+):

- `id` integer.
- `description` string.
- `done` boolean.
- `gap_reason`, `gap_status` optional strings (planner-documented gaps).
- `stage` integer ≥ 1 (default `1`). Groups subgoals into ordered phases
  for the dossier stage ladder; the synth guard refuses to close a
  stage `N+k` subgoal while any stage `N` subgoal is still open. Plans
  loaded from schema 1 jobs deserialise with `stage=1` for every
  subgoal.

### `findings/NNNNNN.md`

Human-readable finding. Shape:

```markdown
# Finding 000001

**Confidence:** 0.85
**Sources:** 1, 2
**Contradicts:** -
**Tags:** finance, q4
**Fragments:** timeline

---

Claim text.
```

### `findings/NNNNNN.json`

Structured finding sidecar:

- `id` integer finding id, matching the filename.
- `claim` string.
- `confidence` number in `[0, 1]`.
- `source_ids` non-empty list of integer source ids.
- `contradicts` list of finding ids or null.
- `tags` list of strings or null.
- `target_fragments` list of canonical report-fragment ids or null.
- `md_path` relative path, normally `findings/NNNNNN.md`.
- `created_at` Unix seconds.

### `sources/<sha256>.md`

The cleaned, searchable source text. The filename is the lowercase SHA-256
hex digest of the cleaned text content.

### `sources/<sha256>.json`

Structured source sidecar:

- `sha256` lowercase hex digest.
- `url` canonical URL string or null.
- `title` source title string or null.
- `fetched_at` Unix seconds.
- `archive_url` Wayback/archive.today URL string or empty string.
- `kind` source kind, matching `research_agent.tools.models.SourceKind`.
- `md_path` relative markdown path, normally `sources/<sha256>.md`.
- `metadata` structured side-info object.

### `synthesis/NNNN.md`

Full synthesis markdown for one synthesis pass. Failed attempts may write
`synthesis/NNNN.failed.md`; those failed files are internal diagnostics and
are not part of the stable synthesis contract.

### `synthesis/NNNN.json`

Structured synthesis sidecar:

- `version` integer, matching the filename.
- `model` model identifier used for the pass.
- `cost_usd` number or null.
- `created_at` Unix seconds.

### `critique/NNNN.md`

Human-readable critique summary for one critique pass.

### `critique/NNNN.json`

Structured critique sidecar:

- `version` integer, matching the filename.
- `model` model identifier.
- `cost_usd` number or null.
- `should_replan` boolean.
- `payload` structured critique output.
- `created_at` Unix seconds.

### `report.md`

Current report markdown. This is the operator-facing artifact. Prior versions
are rotated before rewrite.

### `report.history/*.md`

Archived prior `report.md` bodies. Filenames use UTC
`YYYYMMDDTHHMMSSZ.md`, with `-N` suffixes to avoid collisions.

### `events.jsonl`

Append-only JSON Lines. Each non-empty line validates as
`research_agent.observability.events.Event`:

- `ts` Unix seconds.
- `level` one of `DEBUG`, `INFO`, `WARN`, `ERROR`.
- `actor` string or null.
- `kind` event kind string.
- `payload` object.
- `schema_version` integer, currently `1`.

## Metadata Key Vocabulary

`Source.metadata` is a structured side-info object. The registered shared
keys are:

- `image_url` - canonical image or thumbnail URL for visual sources.
- `image_iiif_manifest` - IIIF manifest URL for image collections.
- `transcript` - transcript text only when the connector also places
  retrieval-relevant text in `cleaned_text`.
- `license` - full rights/license statement.
- `license_short` - short license label such as `CC BY-SA 4.0`.
- `lang` - BCP-47 or connector-native language code.
- `pub_date` - publication date string when available.
- `archive_url` - archive URL when the connector reports it as metadata
  rather than the top-level `Source.archive_url`.
- `mediatype` - source media category such as `texts`, `audio`, `movies`,
  `web`, `image`, or `video`.
- `doi` - Digital Object Identifier.
- `isbn` - International Standard Book Number.
- `oclc` - OCLC identifier.
- `lccn` - Library of Congress Control Number.
- `ein` - Employer Identification Number.

Connector-specific keys are also registered here. Current examples include:

- Cultural/archive connectors: `provider`, `data_provider`, `object_url`,
  `is_shown_at`, `edmIsShownAt`, `edmPreview`, `europeana_id`, `dpla_id`,
  `nara_record_id`, `collection`, `record_group`, `repository`,
  `iiif_manifest`, `fulltext_url`, `audio_files`.
- Scholarly/book connectors: `openalex_id`, `authors`, `host_venue`,
  `pub_year`, `citation_count`, `open_access_url`, `edition_key`,
  `work_key`.
- Video/audio connectors: `video_id`, `duration`, `program_id`,
  `recorded_at`, `speaker`, `channel`.
- Public-record connectors: `filings`, `filing_url`, `filing_type`,
  `candidate_id`, `committee_id`, `award_id`, `recipient_id`,
  `docket_id`, `opinion_id`, `list_kind`, `sanctioning_agency`,
  `jurisdiction`, `entity_number`, `license_number`, `rating`.
- Connector diagnostics: `source_kind`, `source_engine`, `fetched_via`,
  `api_url`, `cache_path`.

Adding any new `Source.metadata` key requires updating this section in the
same PR that introduces the key.

## Cleaned Text Policy

Searchable or retrieval-relevant text goes in `Source.cleaned_text`, which
is materialized as `sources/<sha256>.md`. `Source.metadata` is for structured
side-info that complements `cleaned_text`; it must not replace it.

Connectors that put transcripts, OCR output, page body text, or extracted
article text only in metadata are bugs. If the text should be searched,
embedded, cited, or synthesized, it belongs in `Source.cleaned_text`.

## Python Readers

`research_agent.contract` exposes read-only Pydantic readers:

- `read_job(path) -> JobMetadata`
- `iter_findings(path) -> Iterable[Finding]`
- `read_report(path) -> Report`
- `tail_events(path) -> Iterable[Event]`
- `read_source(path) -> Source`

These functions never mutate the folder and never require the SQLite index.
