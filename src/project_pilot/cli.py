"""Typer command-line interface for project-pilot.

Commands are wired in as features land: ``init-db``, ``run-once``, ``daemon``,
``test-notify``, ``test-filter`` and ``stats``.
"""

import typer

app = typer.Typer(
    name="project-pilot",
    help="Personal freelancermap.de listing pilot.",
    no_args_is_help=True,
    add_completion=False,
)


if __name__ == "__main__":
    app()
