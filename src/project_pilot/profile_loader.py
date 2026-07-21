"""ProfileService: loads profile.md and constraints.yaml, computes profile_hash."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from project_pilot.errors import ConfigError


class ProfileConstraints(BaseModel):
    """Deterministic hard rules from constraints.yaml (the stage-2 input)."""

    blacklist: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=lambda: ["de", "en"])


@dataclass(frozen=True, slots=True)
class Profile:
    """The in-memory profile loaded at boot; ``profile_hash`` versions each verdict."""

    text: str
    constraints: ProfileConstraints
    profile_hash: str


class ProfileService:
    """Loads and validates the two versioned profile files from ``profile_dir``."""

    def __init__(self, profile_dir: Path) -> None:
        self._profile_path = profile_dir / "profile.md"
        self._constraints_path = profile_dir / "constraints.yaml"

    def load(self) -> Profile:
        """Read both files, validate constraints, and hash the raw contents."""
        text = self._read(self._profile_path)
        raw_constraints = self._read(self._constraints_path)
        constraints = self._parse_constraints(raw_constraints)

        digest = hashlib.sha256()
        digest.update(text.encode("utf-8"))
        digest.update(b"\n--constraints--\n")
        digest.update(raw_constraints.encode("utf-8"))

        return Profile(text=text, constraints=constraints, profile_hash=digest.hexdigest())

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as err:
            raise ConfigError(f"cannot read profile file {path}: {err}") from err

    @staticmethod
    def _parse_constraints(raw: str) -> ProfileConstraints:
        try:
            data = yaml.safe_load(raw) or {}
        except yaml.YAMLError as err:
            raise ConfigError(f"constraints.yaml is not valid YAML: {err}") from err
        if not isinstance(data, dict):
            raise ConfigError("constraints.yaml must be a mapping at the top level")
        try:
            return ProfileConstraints.model_validate(data)
        except ValidationError as err:
            raise ConfigError(f"constraints.yaml failed validation: {err}") from err
