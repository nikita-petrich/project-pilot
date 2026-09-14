"""ProfileService: the public half from sequenz.io, the private half from the repo.

The profile used to be a file in this repository. It is now the website's, because
a profile maintained in two places is a profile that is wrong in one of them — and
the wrong one is always the copy nobody looks at. What the site cannot carry stays
here: the industries refused outright and the technologies refused as one's own
job (``profile/private.yaml``), which are statements about clients rather than
about skills and do not belong on a company homepage.

``profile_hash`` is unchanged in kind — a content hash — but it now hashes what was
actually fetched, so a verdict stored under it can still be traced to the profile
it was judged against (``profile_snapshots``). A failed fetch raises rather than
falling back to an older copy: a silent fallback would file today's verdict under
yesterday's profile.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, Field, ValidationError

from project_pilot.errors import ConfigError
from project_pilot.profile_source import RemoteProfile, derived_sections

# The "## Contact & Signature" section is the single source for the applicant's own
# data (name, phone, links). Anything that needs it (signature, enrichment
# messages) reads it from here. The section is rendered from the website's JSON
# feed, not parsed out of prose — see profile_source.derived_sections.
_SIGNATURE_HEADING_RE = re.compile(r"^##\s+Contact\s*&\s*Signature\s*$", re.IGNORECASE)


class ProfileConstraints(BaseModel):
    """Deterministic rules from private.yaml (stage 2, plus the stage-3 no-go guard)."""

    blacklist: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=lambda: ["de", "en"])
    # Context-dependent no-go technologies. Not matched against the listing text
    # (that is what `blacklist` is for) but against the LLM's own
    # `missing_requirements` — see `evaluation/nogo.py`.
    nogo_technologies: list[str] = Field(default_factory=list)
    # The no-go prose, appended to the fetched profile so the LLM judges against
    # the whole picture while the website shows only the public half.
    no_gos: str = ""


def _signature_lines(text: str) -> list[str]:
    """The content lines of the Contact & Signature section (quotes/blanks dropped).

    The **last** such section wins. The public half is a document the website
    generates and may well introduce a section of a similar name; the block this
    reads is appended after it, from values that are exact. Taking the first match
    would let the site quietly decide what goes into an outgoing signature.
    """
    lines: list[str] = []
    inside = False
    for raw in text.splitlines():
        line = raw.strip()
        if _SIGNATURE_HEADING_RE.match(line):
            inside, lines = True, []  # a later section replaces an earlier one
            continue
        if inside and line.startswith("## "):  # next section starts
            inside = False
            continue
        if inside and line and not line.startswith(">"):
            lines.append(line)
    return lines


@dataclass(frozen=True, slots=True)
class Profile:
    """The in-memory profile loaded at boot; ``profile_hash`` versions each verdict."""

    text: str
    constraints: ProfileConstraints
    profile_hash: str
    #: Where the public half came from, for the snapshot record and for logs.
    source_url: str = ""

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


class ProfileSource(Protocol):
    """What ProfileService needs of the website (see profile_source.WebProfileSource)."""

    async def fetch(self) -> RemoteProfile: ...


class ProfileService:
    """Assembles one profile from the live website and the repo's private half."""

    def __init__(self, profile_dir: Path, source: ProfileSource) -> None:
        self._private_path = profile_dir / "private.yaml"
        self._source = source

    async def load(self) -> Profile:
        """Fetch the public half, append the private one, hash the result.

        Order matters only in that it is stable: the same website and the same
        private file must always produce the same hash, or every restart would
        look like a new profile in ``profile_snapshots``.
        """
        raw_private = self._read(self._private_path)
        constraints = self._parse_constraints(raw_private)
        remote = await self._source.fetch()

        text = "\n\n".join(
            part.strip()
            for part in (remote.markdown, derived_sections(remote.feed), constraints.no_gos)
            if part.strip()
        )

        digest = hashlib.sha256()
        digest.update(text.encode("utf-8"))
        digest.update(b"\n--constraints--\n")
        digest.update(raw_private.encode("utf-8"))

        return Profile(
            text=text,
            constraints=constraints,
            profile_hash=digest.hexdigest(),
            source_url=remote.source_url,
        )

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
            raise ConfigError(f"private.yaml is not valid YAML: {err}") from err
        if not isinstance(data, dict):
            raise ConfigError("private.yaml must be a mapping at the top level")
        try:
            return ProfileConstraints.model_validate(data)
        except ValidationError as err:
            raise ConfigError(f"private.yaml failed validation: {err}") from err
