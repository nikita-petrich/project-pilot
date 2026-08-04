"""Tests for ProfileService."""

from pathlib import Path

import pytest

from project_pilot.errors import ConfigError
from project_pilot.profile_loader import ProfileService


def _write_profile(tmp_path: Path, *, profile: str, constraints: str) -> None:
    (tmp_path / "profile.md").write_text(profile, encoding="utf-8")
    (tmp_path / "constraints.yaml").write_text(constraints, encoding="utf-8")


def test_load_returns_profile(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        profile="# Me\nPython engineer.",
        constraints="blacklist:\n  - wordpress\nmust_have:\n  - python\nlanguages: [de, en]\n",
    )
    profile = ProfileService(tmp_path).load()
    assert "Python engineer." in profile.text
    assert profile.constraints.blacklist == ["wordpress"]
    assert profile.constraints.must_have == ["python"]
    assert profile.constraints.languages == ["de", "en"]
    assert len(profile.profile_hash) == 64


def test_constraints_defaults_when_omitted(tmp_path: Path) -> None:
    _write_profile(tmp_path, profile="x", constraints="blacklist: []\n")
    profile = ProfileService(tmp_path).load()
    assert profile.constraints.must_have == []
    assert profile.constraints.languages == ["de", "en"]


def test_hash_changes_with_content(tmp_path: Path) -> None:
    _write_profile(tmp_path, profile="A", constraints="blacklist: []\n")
    first = ProfileService(tmp_path).load().profile_hash
    _write_profile(tmp_path, profile="B", constraints="blacklist: []\n")
    second = ProfileService(tmp_path).load().profile_hash
    assert first != second


def test_hash_stable_for_same_content(tmp_path: Path) -> None:
    _write_profile(tmp_path, profile="A", constraints="blacklist: []\n")
    first = ProfileService(tmp_path).load().profile_hash
    second = ProfileService(tmp_path).load().profile_hash
    assert first == second


def test_missing_file_raises(tmp_path: Path) -> None:
    (tmp_path / "profile.md").write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError):
        ProfileService(tmp_path).load()


def test_invalid_constraints_type_raises(tmp_path: Path) -> None:
    _write_profile(tmp_path, profile="x", constraints="blacklist: not-a-list\n")
    with pytest.raises(ConfigError):
        ProfileService(tmp_path).load()


def test_non_mapping_yaml_raises(tmp_path: Path) -> None:
    _write_profile(tmp_path, profile="x", constraints="- just\n- a\n- list\n")
    with pytest.raises(ConfigError):
        ProfileService(tmp_path).load()


_SIGNATURE_PROFILE = """# Me

Python engineer.

## Contact & Signature

> Value source for the signature block.

Nikita Petrich
Senior Full-Stack & AI Engineer
Phone: +49 1567 9088678
Email: n.petrich@sequenz.io
Web: https://sequenz.io

## Next section

Other content.
"""


def test_contact_values_come_from_the_signature_section(tmp_path: Path) -> None:
    _write_profile(tmp_path, profile=_SIGNATURE_PROFILE, constraints="{}")
    profile = ProfileService(tmp_path).load()
    # first plain line = the applicant's name; quote lines and Key: lines are not it
    assert profile.applicant_name() == "Nikita Petrich"
    assert profile.contact_value("Phone") == "+49 1567 9088678"
    assert profile.contact_value("Web") == "https://sequenz.io"
    assert profile.contact_value("VAT ID") is None  # absent key


def test_contact_values_missing_section_yield_none(tmp_path: Path) -> None:
    _write_profile(tmp_path, profile="# Me\nPython engineer.", constraints="{}")
    profile = ProfileService(tmp_path).load()
    assert profile.applicant_name() is None
    assert profile.contact_value("Phone") is None
