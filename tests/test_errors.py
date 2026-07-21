"""Tests for the domain error hierarchy and assert_defined."""

import pytest

from project_pilot.errors import (
    ConfigError,
    LlmSchemaError,
    ProjectPilotError,
    SelectorMismatchError,
    SourceBlockedError,
    assert_defined,
)


def test_assert_defined_returns_value() -> None:
    assert assert_defined(5, "must be set") == 5
    assert assert_defined("x", "must be set") == "x"


def test_assert_defined_raises_on_none() -> None:
    with pytest.raises(ProjectPilotError, match="must be set"):
        assert_defined(None, "must be set")


@pytest.mark.parametrize(
    "err",
    [ConfigError, SourceBlockedError, SelectorMismatchError, LlmSchemaError],
)
def test_domain_errors_subclass_base(err: type[ProjectPilotError]) -> None:
    assert issubclass(err, ProjectPilotError)
