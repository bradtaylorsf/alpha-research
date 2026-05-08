"""Shared connector exception types.

Connectors raise :class:`MissingCredentialError` when a required env var
(API token, broker key, contact-bearing User-Agent, etc.) is unset. The
orchestrator's connector handlers catch *only* this typed sentinel and
convert it into :class:`~research_agent.orchestrator.errors.FatalError`
— preserving the documented smoke-skip contract (``exit 0`` with
"would need ENV_VAR; live test skipped") while letting any other
``RuntimeError`` from inside a connector propagate to the loop's
catch-all so real bugs surface as ``daemon/error`` events with
tracebacks instead of masquerading as missing credentials.
"""

from __future__ import annotations


class MissingCredentialError(RuntimeError):
    """Raised by a connector when a required credential/config value is unset."""
