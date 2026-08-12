"""ProfileService: loads profile.md and constraints.yaml, computes profile_hash."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from project_pilot.errors import ConfigError

# The "## Contact & Signature" section of profile.md is the single source for the
# applicant's own data (name, phone, links). Anything that needs it (signature,
# enrichment messages) reads it from here instead of duplicating it in ENV.
_SIGNATURE_HEADING_RE = re.compile(r"^##\s+Contact\s*&\s*Signature\s*$", re.IGNORECASE)


class ProfileConstraints(BaseModel):
    """Deterministic rules from constraints.yaml (stage 2, plus the stage-3 no-go guard)."""

    blacklist: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=lambda: ["de", "en"])
    # Context-dependent no-go technologies. Not matched against the listing text
    # (that is what `blacklist` is for) but against the LLM's own
    # `missing_requirements` — see `evaluation/nogo.py`.
    nogo_technologies: list[str] = Field(default_factory=list)


def _signature_lines(text: str) -> list[str]:
    """The content lines of the Contact & Signature section (quotes/blanks dropped)."""
    lines: list[str] = []
    inside = False
    for raw in text.splitlines():
        line = raw.strip()
        if _SIGNATURE_HEADING_RE.match(line):
            inside = True
            continue
        if inside and line.startswith("## "):  # next section starts
            break
        if inside and line and not line.startswith(">"):
            lines.append(line)
    return lines


@dataclass(frozen=True, slots=True)
class Profile:
    """The in-memory profile loaded at boot; ``profile_hash`` versions each verdict."""

    text: str
    constraints: ProfileConstraints
    profile_hash: str

    def applicant_name(self) -> str | None:
        """The applicant's own name: the first plain line of Contact & Signature."""
        for line in _signature_lines(self.text):
            if ":" not in line:  # "Key: value" lines are fields, not the name
                return line
        return None

    def contact_value(self, key: str) -> str | None:
        """The value of a ``Key: value`` line (e.g. ``Phone``) in Contact & Signature."""
        prefix = f"{key.lower()}:"
        for line in _signature_lines(self.text):
            if line.lower().startswith(prefix):
                value = line[len(prefix) :].strip()
                return value or None
        return None


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
