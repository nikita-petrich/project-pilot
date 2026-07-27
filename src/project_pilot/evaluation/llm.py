"""Stage 3 LLM matching via OpenAI structured outputs."""

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Protocol

from openai import AsyncOpenAI

from project_pilot.errors import ConfigError
from project_pilot.evaluation.schemas import MatchVerdict
from project_pilot.ingestion.parser import ParsedListing
from project_pilot.models import Listing

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam

PROMPT_VERSION = "match.v1"
_PROMPTS_DIR = Path(__file__).parent / "prompts"


@dataclass(frozen=True, slots=True)
class LlmResponse:
    """What a structured LLM client returns: a parsed verdict (or None) plus tokens."""

    verdict: MatchVerdict | None
    tokens_in: int | None
    tokens_out: int | None


class StructuredLlmClient(Protocol):
    async def complete(self, *, model: str, system: str, user: str) -> LlmResponse: ...


@dataclass(frozen=True, slots=True)
class LlmEvaluation:
    verdict: MatchVerdict
    model: str
    prompt_version: str
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int
    is_error: bool

    @property
    def score(self) -> int:
        return self.verdict.score

    @property
    def is_match(self) -> bool:
        return self.verdict.verdict == "match"

    def reason(self) -> dict[str, object]:
        data: dict[str, object] = {
            "verdict": self.verdict.verdict,
            "score": self.verdict.score,
            "reasons": list(self.verdict.reasons),
            "matching_skills": list(self.verdict.matching_skills),
            "missing_requirements": list(self.verdict.missing_requirements),
            "risk_flags": list(self.verdict.risk_flags),
        }
        if self.is_error:
            data["error"] = "llm_error"
        return data


def load_prompt(version: str = PROMPT_VERSION) -> str:
    path = _PROMPTS_DIR / f"{version}.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as err:
        raise ConfigError(f"cannot read prompt {path}: {err}") from err


def render_listing(listing: ParsedListing) -> str:
    """Format a parsed listing into the text block handed to the model."""
    parts = [f"Title: {listing.title}", f"Remote: {listing.remote_status.value}"]
    if listing.location:
        parts.append(f"Location: {listing.location}")
    if listing.start_asap:
        parts.append("Start: ab sofort")
    elif listing.start_date is not None:
        parts.append(f"Start: {listing.start_date.isoformat()}")
    if listing.skills:
        parts.append("Skills: " + ", ".join(listing.skills))
    parts.append("")
    parts.append(listing.description)
    return "\n".join(parts)


def render_listing_entity(listing: Listing) -> str:
    """Format a stored listing into the text block handed to the model."""
    raw = listing.raw or {}
    parts = [f"Title: {listing.title}"]
    company = raw.get("company")
    if isinstance(company, str) and company:
        parts.append(f"Company: {company}")
    contact = " ".join(
        part for part in (raw.get("firstName"), raw.get("lastName")) if isinstance(part, str)
    ).strip()
    if contact:
        parts.append(f"Contact: {contact}")
    parts.append(f"Remote: {listing.remote_status.value}")
    if listing.location:
        parts.append(f"Location: {listing.location}")
    if listing.start_asap:
        parts.append("Start: ab sofort")
    elif listing.start_date is not None:
        parts.append(f"Start: {listing.start_date.isoformat()}")
    if listing.skills:
        parts.append("Skills: " + ", ".join(listing.skills))
    parts.append("")
    parts.append(listing.description)
    return "\n".join(parts)


def is_match_notifiable(evaluation: LlmEvaluation, threshold: int) -> bool:
    return evaluation.is_match and evaluation.score >= threshold


class LlmMatcher:
    """Runs stage 3: one call, one retry on an invalid parse, then an llm_error fallback."""

    def __init__(
        self,
        client: StructuredLlmClient,
        *,
        model: str,
        prompt_template: str,
        prompt_version: str = PROMPT_VERSION,
    ) -> None:
        self._client = client
        self._model = model
        self._prompt_template = prompt_template
        self._prompt_version = prompt_version

    async def evaluate(self, *, profile_text: str, listing_text: str) -> LlmEvaluation:
        user = f"## Candidate profile\n{profile_text}\n\n## Project listing\n{listing_text}"
        started = perf_counter()
        detail = "no response"
        for _ in range(2):
            try:
                response = await self._client.complete(
                    model=self._model, system=self._prompt_template, user=user
                )
            except Exception as err:
                detail = f"llm call failed: {err}"
                continue
            if response.verdict is not None:
                return LlmEvaluation(
                    verdict=response.verdict,
                    model=self._model,
                    prompt_version=self._prompt_version,
                    tokens_in=response.tokens_in,
                    tokens_out=response.tokens_out,
                    latency_ms=_elapsed_ms(started),
                    is_error=False,
                )
            detail = "schema violation (empty parse)"
        return LlmEvaluation(
            verdict=MatchVerdict.llm_error_fallback(detail),
            model=self._model,
            prompt_version=self._prompt_version,
            tokens_in=None,
            tokens_out=None,
            latency_ms=_elapsed_ms(started),
            is_error=True,
        )


class OpenAiStructuredClient:
    """Thin adapter over the OpenAI SDK's structured `parse` (network, not unit-tested)."""

    def __init__(
        self, api_key: str, *, client: AsyncOpenAI | None = None
    ) -> None:  # pragma: no cover
        self._client = client or AsyncOpenAI(api_key=api_key)

    async def complete(
        self, *, model: str, system: str, user: str
    ) -> LlmResponse:  # pragma: no cover
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        completion = await self._client.chat.completions.parse(
            model=model,
            messages=messages,
            response_format=MatchVerdict,
        )
        message = completion.choices[0].message
        usage = completion.usage
        return LlmResponse(
            verdict=message.parsed,
            tokens_in=usage.prompt_tokens if usage is not None else None,
            tokens_out=usage.completion_tokens if usage is not None else None,
        )


def _elapsed_ms(started: float) -> int:
    return int((perf_counter() - started) * 1000)
