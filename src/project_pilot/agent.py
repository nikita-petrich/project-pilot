"""The agent that answers inside a match topic.

A full Claude Code agent, not a narrowed one: shell, filesystem, search and the
web are all there, because the work in a match thread is not only "call a tool"
— it is reading an attachment, checking a fact, trying something out. What the
agent must *not* do is invent Nik's profile, the judging rules or the writing
style, so project-pilot's own MCP server is attached and the system prompt sends
every domain question through it.

The Claude Agent SDK runs the loop in this process (it ships its own Claude Code
binary, so there is nothing to install alongside it) and writes each topic's
transcript to ``CLAUDE_CONFIG_DIR``. We keep only the session id per topic in
Postgres and resume by it, so the conversation survives a restart without us
replaying a history we would then have to keep in sync.
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    Message,
    ResultMessage,
    TextBlock,
    query,
)

logger = logging.getLogger(__name__)

MCP_SERVER = "project_pilot"
# One message may fan out into reading, drafting and a check; past this it is a
# loop, not work. The cap is per message, and the user can simply say "weiter".
MAX_TURNS = 60
MAX_BUDGET_USD = 5.0

SYSTEM = """\
Du bist project-pilots Assistent im Telegram-Thread zu genau einem Projekt.

Dieser Thread gehört zu **Listing {listing_id}**. Wenn keine andere Nummer
genannt wird, ist das die gemeinte.

Nik's Profil, die Urteilsregeln und der Bewerbungs-Stil liegen hinter den
`mcp__project_pilot__*`-Tools. Alles Fachliche geht durch sie, nicht aus dem
Gedächtnis, und du erfindest keine Fakten über Nik oder das Projekt:
- Prüfen: `project_pilot_check_listing` — deren Urteil gilt, überstimme es nicht
  mit deiner eigenen Lesart der Ausschreibung.
- Entwerfen: `project_pilot_draft_application`, Änderungen über
  `project_pilot_revise_application`.
- Kontaktdaten: `project_pilot_enrich_company`.
- Empfänger: `project_pilot_set_recipient`.

Shell, Dateisystem und Websuche stehen dir frei zur Verfügung; nutze sie für
alles andere. Dein Arbeitsverzeichnis ist beschreibbar und bleibt bestehen.

**Senden ist die einzige unumkehrbare Handlung.** Rufe
`project_pilot_send_application` erst, wenn du Empfänger und Betreff gezeigt und
Nik in *dieser* Unterhaltung ausdrücklich zugestimmt hat. Ein früheres „mach
mal" zählt nicht. Biete das Senden nicht von dir aus an, und verschicke nie auf
einem anderen Weg (kein Mail-Client, kein SMTP von Hand).

Antworte auf Deutsch, knapp und ohne Floskeln. Du schreibst in Telegram: kein
Markdown, keine Überschriften, kurze Absätze. Nenne die `listing_id` oder
`application_id`, wenn sie für den nächsten Schritt gebraucht wird.
"""


class Runner(Protocol):
    """The SDK's ``query`` narrowed to what this module calls."""

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]: ...


@dataclass(frozen=True, slots=True)
class AgentReply:
    """What the agent produced, and the session to continue it in."""

    text: str
    ok: bool
    session_id: str | None = None


def _text_of(messages: list[Message]) -> str:
    """The final answer: the result line, or the assistant's own prose."""
    for message in reversed(messages):
        if isinstance(message, ResultMessage) and message.result:
            return message.result.strip()
    parts = [
        block.text
        for message in messages
        if isinstance(message, AssistantMessage)
        for block in message.content
        if isinstance(block, TextBlock) and block.text
    ]
    return "\n\n".join(parts).strip()


class ThreadAgent:
    """Answers one message in one topic, continuing that topic's session."""

    def __init__(
        self,
        *,
        api_key: str,
        mcp_url: str,
        workspace: Path,
        model: str = "claude-opus-5",
        runner: Runner | None = None,
    ) -> None:
        # The SDK reads ANTHROPIC_API_KEY from the subprocess environment, so the
        # key is passed through `env` rather than to a client object.
        self._api_key = api_key
        self._mcp_url = mcp_url
        self._workspace = workspace
        self._model = model
        # `runner` exists so tests can drive the loop without spawning the CLI.
        self._run: Runner = runner or _default_runner

    def _options(self, *, listing_id: int, session_id: str | None) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            model=self._model,
            cwd=self._workspace,
            # Append to Claude Code's own prompt: the built-in tools keep their
            # instructions, and this adds what is specific to a match thread.
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": SYSTEM.format(listing_id=listing_id),
            },
            mcp_servers={MCP_SERVER: {"type": "http", "url": self._mcp_url}},
            # Nobody is at a terminal to answer a permission prompt, and the
            # whitelist upstream already decided who may talk to this agent.
            permission_mode="bypassPermissions",
            # No skills, commands or settings off the image's filesystem: the
            # repo's `.claude/` holds the build workflow, which has no business
            # in a match thread. Judgment comes from the MCP server.
            setting_sources=[],
            max_turns=MAX_TURNS,
            max_budget_usd=MAX_BUDGET_USD,
            resume=session_id,
            env={"ANTHROPIC_API_KEY": self._api_key},
        )

    async def reply(self, *, listing_id: int, session_id: str | None, message: str) -> AgentReply:
        """One turn in this topic's session; returns the session to continue in.

        Never raises: a failing run has to reach the user as a sentence in the
        thread, not as a silent gap.
        """
        try:
            return await self._once(listing_id=listing_id, session_id=session_id, message=message)
        except ClaudeSDKError as err:
            if session_id is None:
                logger.warning("agent run failed for listing %s: %s", listing_id, err)
                return AgentReply(text=f"⚠️ Der Assistent ist gerade gescheitert ({err}).", ok=False)
            # The usual cause is a transcript that is no longer on this host —
            # a redeployed container, a wiped volume. Starting over beats
            # answering nothing; the tools still hold every fact.
            logger.warning("resume failed for listing %s, starting fresh: %s", listing_id, err)
            try:
                return await self._once(listing_id=listing_id, session_id=None, message=message)
            except ClaudeSDKError as retry_err:
                logger.warning("agent run failed for listing %s: %s", listing_id, retry_err)
                return AgentReply(
                    text=f"⚠️ Der Assistent ist gerade gescheitert ({retry_err}).", ok=False
                )

    async def _once(self, *, listing_id: int, session_id: str | None, message: str) -> AgentReply:
        messages: list[Message] = []
        async for item in self._run(
            prompt=message,
            options=self._options(listing_id=listing_id, session_id=session_id),
        ):
            messages.append(item)
        text = _text_of(messages)
        started = next(
            (m.session_id for m in reversed(messages) if isinstance(m, ResultMessage)),
            session_id,
        )
        if not text:
            # A run that ends without prose would look like the bot ignored the
            # message; the session id is still worth keeping.
            return AgentReply(
                text="⚠️ Der Assistent hat nichts geantwortet. Frag noch einmal.",
                ok=False,
                session_id=started,
            )
        return AgentReply(text=text, ok=True, session_id=started)


def _default_runner(*, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
    return query(prompt=prompt, options=options)
