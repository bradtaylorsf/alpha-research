"""Typer entry point — `research ...` command surface (implementation guide §4, §5)."""

from __future__ import annotations

import typer

from research_agent import config

config.load_env()

app = typer.Typer(
    name="research",
    help="Autonomous CLI research agent.",
    no_args_is_help=True,
)


@app.callback()
def _main() -> None:
    """Top-level callback — subcommands land here once registered."""


if __name__ == "__main__":
    app()
