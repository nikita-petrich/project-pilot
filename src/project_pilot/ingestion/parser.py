"""freelancermap list/detail parsing from the embedded react-on-rails JSON blobs.

The site renders its project data server-side into
``<script class="js-react-on-rails-component" data-component-name="...">`` blobs
(``ProjectSearch`` on list pages, ``ProjectShow`` on detail pages). Parsing the
structured JSON is more robust than CSS scraping; the blob names and the field
subset below are the one place to adjust when the source structure changes.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from project_pilot.errors import SelectorMismatchError
from project_pilot.ingestion.normalize import (
    canonicalize_url,
    compute_url_hash,
    html_to_text,
    parse_posted,
    remote_status_from_percent,
    start_from_parts,
)
from project_pilot.models import PostedPrecision, RemoteStatus

# --- react-on-rails blobs: the one place to adjust when the source structure changes ---
# Pagination has no server-rendered <a> (the page links live only inside the blob),
# so the pipeline walks pages by incrementing pagenr (see normalize.next_page_url).
# REACT_MARKER also serves the client's captcha heuristic: a page carrying the
# payload is real content, not a challenge wall.
REACT_MARKER = "js-react-on-rails-component"
_REACT_COMPONENT = f"script.{REACT_MARKER}"
LIST_COMPONENT = "ProjectSearch"
DETAIL_COMPONENT = "ProjectShow"


class _ListItem(BaseModel):
    """One entry from ``ProjectSearch.initialResults`` on a list page."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    slug: str
    title: str
    created: str | None = None


class _ProjectSearch(BaseModel):
    """The list-page react component; only the fields we consume."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    initial_results: list[_ListItem] = Field(default_factory=list, alias="initialResults")


class _Country(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name_de: str | None = Field(default=None, alias="nameDe")


class _ContractType(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    remote_in_percent: int | None = Field(default=None, alias="remoteInPercent")


class _Skill(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    localized_name: str | None = Field(default=None, alias="localizedName")
    name_de: str | None = Field(default=None, alias="nameDe")


class _Skills(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    enabled: list[_Skill] = Field(default_factory=list)


class _Project(BaseModel):
    """The detail-page project record; only the fields we map to a listing."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    title: str
    created: str | None = None
    start_year: int | None = Field(default=None, alias="startYear")
    start_month: int | None = Field(default=None, alias="startMonth")
    start_text: str | None = Field(default=None, alias="startText")
    city: str | None = None
    country: _Country | None = None
    contract_type: _ContractType | None = Field(default=None, alias="contractType")
    skills: _Skills | None = None
    description: str | None = None
    display_description: str | None = Field(default=None, alias="displayDescription")


class _ProjectShow(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    project: _Project


@dataclass(frozen=True, slots=True)
class ListingSummary:
    """One project from a list page: enough to dedupe and gate on freshness."""

    external_url: str
    url_hash: str
    title: str
    posted_at: datetime | None
    posted_at_precision: PostedPrecision


@dataclass(frozen=True, slots=True)
class ParsedListing:
    """A fully parsed detail page, ready to become a Listing row."""

    source: str
    external_url: str
    url_hash: str
    title: str
    description: str
    skills: list[str]
    start_date: date | None
    start_asap: bool
    end_date: date | None
    location: str | None
    remote_status: RemoteStatus
    posted_at: datetime | None
    posted_at_precision: PostedPrecision
    raw: dict[str, object]


def _react_json(html: str, name: str) -> dict[str, object]:
    """Return the raw JSON object of an embedded react-on-rails component by name.

    Raises ``SelectorMismatchError`` (loud, never silent) when the script tag is
    absent, is not valid JSON, or is not a JSON object.
    """
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one(f'{_REACT_COMPONENT}[data-component-name="{name}"]')
    if node is None:
        raise SelectorMismatchError(f"react-on-rails component {name!r} not found")
    raw = node.string or node.get_text()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        raise SelectorMismatchError(f"react-on-rails component {name!r} is not valid JSON") from err
    if not isinstance(data, dict):
        raise SelectorMismatchError(f"react-on-rails component {name!r} is not a JSON object")
    return data


def _validate[T: BaseModel](data: dict[str, object], name: str, model: type[T]) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as err:
        raise SelectorMismatchError(f"react-on-rails component {name!r} failed validation") from err


def _location(city: str | None, country: str | None) -> str | None:
    parts = [part for part in (city, country) if part]
    return ", ".join(parts) or None


def parse_list_page(html: str, base_url: str) -> list[ListingSummary]:
    """Parse a list page's ``ProjectSearch`` blob into summaries (empty is valid)."""
    search = _validate(_react_json(html, LIST_COMPONENT), LIST_COMPONENT, _ProjectSearch)
    summaries: list[ListingSummary] = []
    for item in search.initial_results:
        url = canonicalize_url(f"/projekt/{item.slug}", base_url)
        posted_at, precision = parse_posted(item.created, None)
        summaries.append(
            ListingSummary(
                external_url=url,
                url_hash=compute_url_hash(url),
                title=item.title,
                posted_at=posted_at,
                posted_at_precision=precision,
            )
        )
    return summaries


def parse_detail_page(html: str, base_url: str, *, source: str, external_url: str) -> ParsedListing:
    """Parse a detail page's ``ProjectShow`` blob into a fully populated listing."""
    data = _react_json(html, DETAIL_COMPONENT)
    project = _validate(data, DETAIL_COMPONENT, _ProjectShow).project

    start_date, start_asap = start_from_parts(
        project.start_year, project.start_month, project.start_text
    )
    posted_at, precision = parse_posted(project.created, None)
    remote_percent = project.contract_type.remote_in_percent if project.contract_type else None
    country = project.country.name_de if project.country else None
    enabled = project.skills.enabled if project.skills else []
    skills = [name for skill in enabled if (name := skill.localized_name or skill.name_de)]

    raw_project = data["project"]
    url = canonicalize_url(external_url, base_url)
    return ParsedListing(
        source=source,
        external_url=url,
        url_hash=compute_url_hash(url),
        title=project.title,
        description=html_to_text(project.description or project.display_description or ""),
        skills=skills,
        start_date=start_date,
        start_asap=start_asap,
        end_date=None,
        location=_location(project.city, country),
        remote_status=remote_status_from_percent(remote_percent),
        posted_at=posted_at,
        posted_at_precision=precision,
        raw=raw_project if isinstance(raw_project, dict) else {},
    )
