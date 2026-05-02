# CLAUDE.md

## Project: Alpha Research

Research workspace for AI agent investigation, setup, and implementation patterns. The output of this work feeds into agents and tooling built elsewhere in the `Alpha*` ecosystem.

## How we work here

This repo is driven by **alpha-loop** (`../alpha-loop`), which implements:

**Plan (GitHub Issues) → Build (AI Agent) → Test → Review → Ship (PR)**

- **GitHub Issues are the canonical source of truth and the roadmap.** Anything not in an issue does not exist. Before starting work, check open issues; if it isn't there, create it first.
- **Milestones group issues into themes / phases.** Use `alpha-loop roadmap` to organize.
- **PRs close issues.** Don't merge work that isn't tied to an issue.
- **Learnings flow back via `alpha-loop review`** — surprises, corrections, and patterns get captured for future runs.

## Common alpha-loop commands

Run from this directory (or pass `--cwd`):

```bash
alpha-loop init                  # First-time setup: config, templates, scan, sync
alpha-loop scan                  # Refresh project context for the agent
alpha-loop add                   # Create a new issue from a free-form description
alpha-loop triage                # Improve existing issue quality
alpha-loop roadmap               # Organize open issues into milestones
alpha-loop run --once            # Process one issue end-to-end and stop
alpha-loop run                   # Continuous loop
alpha-loop run --epic <N>        # Walk an epic's sub-issues in checklist order
alpha-loop resume --issue <N>    # Pick up a stranded session
alpha-loop history               # Inspect prior runs
alpha-loop review                # Propose agent/skill improvements from learnings
```

See `../alpha-loop/CLAUDE.md` for the full command surface and engine internals.

## Working in this repo as a human / Claude Code

- **Start from an issue.** `gh issue list` first; if nothing fits, `alpha-loop add "<description>"` to create one.
- **Branches:** alpha-loop manages worktrees + branch naming. If working manually, use `issue-<N>-<slug>`.
- **Don't add planning docs as files** — put plans in the issue body or comments. Ad-hoc `*.md` files in the repo root drift; issues don't.
- **Research artifacts** (the existing `*.md` playbooks) are the *content* of the project, not status docs. Keep them tidy and link to them from issues.
- **Commits:** conventional style, reference the issue (`fix: …  (#42)`).

## Conventions

| Thing | Where it lives |
|---|---|
| Roadmap / backlog | GitHub Issues + Milestones |
| Active work | GitHub Issues assigned + in-progress label |
| Decisions | Issue comments / PR descriptions |
| Long-form research | `*.md` files in this repo |
| Loop config | `.alpha-loop.yaml` (created by `alpha-loop init`) |
| Session history | `.alpha-loop/sessions/` (gitignored) |

## Definitions of done

A change is done when:
1. It's tied to an issue.
2. The PR closes that issue (`Closes #N`).
3. CI is green.
4. Any new patterns or surprises are captured via `alpha-loop review` so the next run benefits.
