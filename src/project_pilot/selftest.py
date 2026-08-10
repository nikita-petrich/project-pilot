"""End-to-end smoke test of the alert chain, driven by ``project-pilot test-match``.

The unit suite proves each stage against fakes; this proves the *wiring* against the
real services — profile, hard rules, the LLM, and Slack — by pushing one listing all
the way to a posted message. Nothing is stored and the scan watermark is untouched,
so it is safe to run against production at any time.
"""

import logging
from dataclasses import dataclass
from typing import Protocol

from project_pilot.evaluation.check import CheckResult
from project_pilot.models import EvaluationStage
from project_pilot.notification.messages import MatchMessage
from project_pilot.notification.slack import (
    Block,
    check_fallback_text,
    format_check_blocks,
)

logger = logging.getLogger(__name__)

DEMO_LISTING = """\
Senior Fullstack Entwickler (m/w/d) - NestJS / Next.js / RAG-Plattform

Für den Aufbau einer KI-gestützten Dokumentenplattform im LegalTech-Umfeld suchen
wir ab sofort einen erfahrenen Fullstack-Entwickler.

Aufgaben:
- Aufbau und Betrieb einer RAG-Pipeline (Embeddings, pgvector, semantische Suche)
- Backend-Services mit NestJS und TypeScript, REST- und GraphQL-APIs
- Frontend mit Next.js, React und Tailwind CSS
- Integration der OpenAI API in produktive Workflows
- Clean Architecture, CI/CD über GitHub Actions, Docker

Anforderungen:
- Mehrjährige Erfahrung mit TypeScript, NestJS und Next.js
- Praxiserfahrung mit LLM-Integration und RAG
- PostgreSQL, Docker, sauberer und getesteter Code
- Deutsch verhandlungssicher, Englisch gut

Rahmen: Start ab sofort, Laufzeit 6 Monate mit Option auf Verlängerung,
Auslastung 100 %, 100 % Remote (EU), gelegentliche Abstimmung vor Ort in München.
"""


class Checker(Protocol):
    """The ``CheckService`` subset used here (fakeable in tests)."""

    async def check_text(self, text: str) -> CheckResult: ...

    async def check_stored(self, listing_id: int) -> CheckResult: ...


class Poster(Protocol):
    """The ``SlackClient`` subset used to post a check verdict."""

    async def post_blocks(
        self, blocks: list[Block], text: str, *, thread_ts: str | None = None
    ) -> object | None: ...


class MatchNotifier(Protocol):
    """The ``SlackNotifier`` subset used to post a real match card."""

    async def send_match(self, message: MatchMessage, *, listing_id: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class SelfTestStep:
    """One checked link in the chain, with a human-readable outcome."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class SelfTestReport:
    """The full run: one step per link, plus the verdict that was produced."""

    steps: list[SelfTestStep]
    result: CheckResult | None

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)


class SelfTestService:
    """Pushes one listing through evaluation into Slack and reports every link.

    A diagnostic deliberately reports failures instead of raising them: a broken LLM
    must still yield a report that shows Slack was reached, which is the whole point
    of running it.
    """

    def __init__(
        self,
        *,
        checker: Checker,
        poster: Poster,
        notifier: MatchNotifier,
        profile_hash: str,
    ) -> None:
        self._checker = checker
        self._poster = poster
        self._notifier = notifier
        self._profile_hash = profile_hash

    async def run(
        self, *, text: str | None = None, listing_id: int | None = None
    ) -> SelfTestReport:
        """Evaluate one listing and post the outcome to Slack.

        ``listing_id`` uses a stored listing and posts the real match card, so the
        Apply and Find-contact buttons carry a resolvable id; otherwise ``text`` (or
        the built-in demo) is evaluated and rendered like a manual ``/check``.
        """
        steps = [SelfTestStep("profile", True, f"loaded, hash {self._profile_hash[:12]}")]

        try:
            result = (
                await self._checker.check_stored(listing_id)
                if listing_id is not None
                else await self._checker.check_text(text or DEMO_LISTING)
            )
        except Exception as err:
            logger.exception("self-test evaluation failed")
            steps.append(SelfTestStep("evaluation", False, f"{type(err).__name__}: {err}"))
            return SelfTestReport(steps=steps, result=None)

        steps.append(_evaluation_step(result))
        steps.append(await self._post(result, listing_id))
        return SelfTestReport(steps=steps, result=result)

    async def _post(self, result: CheckResult, listing_id: int | None) -> SelfTestStep:
        """Post the verdict, preferring the real match card when an id is available."""
        try:
            if result.passed and result.message is not None and listing_id is not None:
                sent = await self._notifier.send_match(result.message, listing_id=listing_id)
                detail = "match card + thread detail posted (Apply / Find contact are live)"
            else:
                posted = await self._poster.post_blocks(
                    format_check_blocks(result), check_fallback_text(result)
                )
                sent = posted is not None
                detail = "check verdict posted"
        except Exception as err:
            logger.exception("self-test Slack post failed")
            return SelfTestStep("slack", False, f"{type(err).__name__}: {err}")
        if not sent:
            return SelfTestStep("slack", False, "Slack rejected the message (see the log)")
        return SelfTestStep("slack", True, detail)


def _evaluation_step(result: CheckResult) -> SelfTestStep:
    """Judge the evaluation link: reaching a real verdict is success, a match is not required."""
    if result.is_llm_error:
        return SelfTestStep("evaluation", False, "the LLM returned no usable verdict")
    if result.stage is EvaluationStage.HARD_RULE:
        return SelfTestStep("evaluation", True, "stopped by a hard rule before the LLM")
    outcome = "match" if result.passed else "no match"
    return SelfTestStep(
        "evaluation", True, f"LLM verdict {outcome}, score {result.score}/{result.threshold}"
    )


def format_selftest(report: SelfTestReport) -> str:
    """Render the report as one line per step plus a verdict line."""
    lines = [
        f"{'PASS' if step.ok else 'FAIL'}  {step.name:<11} {step.detail}" for step in report.steps
    ]
    lines += ["", "self-test passed" if report.ok else "self-test FAILED"]
    return "\n".join(lines)
