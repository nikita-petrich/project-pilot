"""Scaffold smoke test: the package and its CLI entry point import cleanly."""

import project_pilot
from project_pilot.cli import app


def test_package_has_version() -> None:
    assert project_pilot.__version__


def test_cli_app_exists() -> None:
    assert app is not None
