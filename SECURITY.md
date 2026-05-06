# Security Policy

## Reporting a vulnerability

If you discover a security vulnerability in this project, please **do not
open a public GitHub issue**. Instead:

- Email the maintainer directly via the contact info on
  [@bradtaylorsf](https://github.com/bradtaylorsf), or
- Use GitHub's [private vulnerability reporting](../../security/advisories/new).

Expect an initial response within a few days. If the issue is real I'll
work on a fix and credit you in the release notes (unless you'd prefer
not to be named).

## Scope

The kinds of issues that warrant a private report:

- Anything that would let an attacker exfiltrate API keys, OpenRouter
  tokens, OpenAI tokens, or operator-stored secrets from a running daemon.
- Sandbox escape from the connector layer (e.g., a malicious URL that
  causes `web_fetch` to read arbitrary local files).
- SQL injection or path traversal against `data/index.sqlite` or
  `jobs/<id>/`.
- Any issue affecting the integrity of stored research artifacts (a
  third party being able to silently rewrite findings or reports).

## Out of scope

- Bugs in connector parsers (HTML / JSON drift) — file these as
  regular issues with the `bug` label.
- Investigation outputs that are factually wrong or politically
  contentious — that's a research-quality issue, not a security issue.
- Local denial-of-service from feeding the agent a goal that's too
  expensive to run — that's a budget/cap configuration issue.

## A note on research artifacts

This tool produces investigative research outputs that may name people,
companies, or organizations. The factual accuracy of those outputs is
the operator's responsibility — the maintainer does not vet what
investigations users run or what the agent reports. If you believe a
public artifact published by an operator of this tool defames you,
please contact the operator directly; this repository hosts the
software, not the published research.
