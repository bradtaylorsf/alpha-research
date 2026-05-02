"""Typer entry point — `research ...` command surface (implementation guide §4, §5)."""

from __future__ import annotations

import typer

from research_agent import __version__, config, doctor

_LOADED_ENV_FILES = config.load_env()

app = typer.Typer(
    name="research",
    help="Autonomous CLI research agent.",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(  # noqa: ARG001 — eager callback handles exit
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
) -> None:
    """Top-level callback — subcommands land here once registered."""


@app.command(name="doctor")
def doctor_command(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON instead of the Rich table.",
    ),
) -> None:
    """Report environment readiness for the research agent."""
    results = doctor.run_all_checks(_LOADED_ENV_FILES)
    if json_output:
        typer.echo(doctor.emit_json(results, _LOADED_ENV_FILES))
    else:
        doctor.render_table(results)
    if doctor.has_required_failure(results):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
