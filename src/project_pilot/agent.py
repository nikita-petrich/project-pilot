"""The agent that answers inside a match topic.

A full Claude Code agent, not a narrowed one: shell, filesystem, search and the
web are all there, because the work in a match thread is not only "call a tool"
— it is reading an attachment, checking a fact, trying something out. What the
agent must *not* do is invent Nik's profile, the judging rules or the writing
style, so project-pilot's own MCP server is attached and the system prompt sends
every domain question through it.

The Claude Agent SDK runs the loop in this process (it ships its own Claude Code
binary, so there is nothing to install alongside it), which also means the MCP
client runs here: the agent talks to the MCP container directly inside the
stack, so nothing about a match thread leaves through the public endpoint. It
writes each topic's
transcript to ``CLAUDE_CONFIG_DIR``. We keep only the session id per topic in
Postgres and resume by it, so the conversation survives a restart without us
replaying a history we would then have to keep in sync.
"""

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    Message,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    query,
)

logger = logging.getLogger(__name__)

MCP_SERVER = "project_pilot"
# One message may fan out into reading, drafting and a check; past this it is a
# loop, not work. The cap is per message, and the user can simply say "weiter".
MAX_TURNS = 60
MAX_BUDGET_USD = 5.0

# Pre-approved: reading, searching, and the domain tools that only look at a
# listing or produce a draft nobody has seen yet. Everything else — writing to
# disk, running a command, naming a recipient, sending — is not listed and
# therefore falls through to the approval callback. This is the one knob: move a
# tool out of here to be asked about it, in here to stop being asked.
ALLOWED_TOOLS = (
    "Read",
    "Glob",
    "Grep",
    "WebSearch",
    "WebFetch",
    "TodoWrite",
    f"mcp__{MCP_SERVER}__project_pilot_get_listing",
    f"mcp__{MCP_SERVER}__project_pilot_list_matches",
    f"mcp__{MCP_SERVER}__project_pilot_check_listing",
    f"mcp__{MCP_SERVER}__project_pilot_check_text",
    f"mcp__{MCP_SERVER}__project_pilot_draft_application",
    f"mcp__{MCP_SERVER}__project_pilot_revise_application",
    f"mcp__{MCP_SERVER}__project_pilot_enrich_company",
)

# What a permission question shows about the call. Long values are cut: the
# question has to fit in a chat bubble and be readable on a phone.
DETAIL_KEYS = ("command", "file_path", "path", "url", "email", "application_id", "listing_id")
DETAIL_CHARS = 300

# Answer within this or the call is refused. An unanswered question must not
# hold a turn open forever.
APPROVAL_TIMEOUT_S = 600

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
Lesen läuft ohne Rückfrage; alles, was schreibt, ausführt oder verschickt, legt
Nik eine Freigabe in den Thread. Lehnt er ab, frag nach — versuch es nicht auf
einem anderen Weg noch einmal.

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


# Asks the human and returns their answer. The agent does not know it is talking
# to Telegram; the bot passes one of these in per message.
Approve = Callable[[str, str], Awaitable[bool]]


async def allow_everything(tool: str, detail: str) -> bool:
    """The default when no human is reachable, e.g. in a smoke test."""
    return True


def describe(tool: str, tool_input: dict[str, Any]) -> str:
    """One short line naming what the call would actually do.

    A bare tool name is not enough to decide on: "Bash" tells you nothing,
    ``rm -rf /data`` tells you everything.
    """
    for key in DETAIL_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str | int) and str(value).strip():
            return _clip(str(value))
    if not tool_input:
        return ""
    return _clip(json.dumps(tool_input, ensure_ascii=False, sort_keys=True))


def _clip(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= DETAIL_CHARS else flat[:DETAIL_CHARS].rstrip() + " …"


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
        mcp_token: str,
        workspace: Path,
        model: str = "claude-opus-5",
        runner: Runner | None = None,
    ) -> None:
        # The SDK reads ANTHROPIC_API_KEY from the subprocess environment, so the
        # key is passed through `env` rather than to a client object.
        self._api_key = api_key
        self._mcp_url = mcp_url
        self._mcp_token = mcp_token
        self._workspace = workspace
        self._model = model
        # `runner` exists so tests can drive the loop without spawning the CLI.
        self._run: Runner = runner or _default_runner

    def _gate(
        self, approve: Approve
    ) -> Callable[
        [str, dict[str, Any], ToolPermissionContext],
        Awaitable[PermissionResultAllow | PermissionResultDeny],
    ]:
        """Turn the human's yes/no into the SDK's permission result."""

        async def can_use_tool(
            tool: str, tool_input: dict[str, Any], context: ToolPermissionContext
        ) -> PermissionResultAllow | PermissionResultDeny:
            detail = describe(tool, tool_input)
            if await approve(tool, detail):
                return PermissionResultAllow()
            # The message reaches the model, so it says what to do next rather
            # than only that something failed.
            return PermissionResultDeny(
                message="Nik hat das abgelehnt. Frag nach, was er stattdessen will."
            )

        return can_use_tool

    def _options(
        self, *, listing_id: int, session_id: str | None, approve: Approve
    ) -> ClaudeAgentOptions:
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
            mcp_servers={
                MCP_SERVER: {
                    "type": "http",
                    "url": self._mcp_url,
                    # A header, not the `/t/<token>` path form: the client runs
                    # here, so the token never has to live in a URL.
                    "headers": {"Authorization": f"Bearer {self._mcp_token}"},
                }
            },
            # Same shape as a Claude session: reading runs, anything that writes,
            # executes or sends asks first. The question goes to the thread the
            # message came from, as two buttons.
            permission_mode="default",
            allowed_tools=list(ALLOWED_TOOLS),
            can_use_tool=self._gate(approve),
            # No skills, commands or settings off the image's filesystem: the
            # repo's `.claude/` holds the build workflow, which has no business
            # in a match thread. Judgment comes from the MCP server.
            setting_sources=[],
            max_turns=MAX_TURNS,
            max_budget_usd=MAX_BUDGET_USD,
            resume=session_id,
            env={"ANTHROPIC_API_KEY": self._api_key},
        )

    async def reply(
        self,
        *,
        listing_id: int,
        session_id: str | None,
        message: str,
        approve: Approve = allow_everything,
    ) -> AgentReply:
        """One turn in this topic's session; returns the session to continue in.

        ``approve`` is asked before every tool that is not pre-approved, and may
        take as long as the human does.

        Never raises: a failing run has to reach the user as a sentence in the
        thread, not as a silent gap.
        """
        try:
            return await self._once(
                listing_id=listing_id, session_id=session_id, message=message, approve=approve
            )
        except ClaudeSDKError as err:
            if session_id is None:
                logger.warning("agent run failed for listing %s: %s", listing_id, err)
                return AgentReply(text=f"⚠️ Der Assistent ist gerade gescheitert ({err}).", ok=False)
            # The usual cause is a transcript that is no longer on this host —
            # a redeployed container, a wiped volume. Starting over beats
            # answering nothing; the tools still hold every fact.
            logger.warning("resume failed for listing %s, starting fresh: %s", listing_id, err)
            try:
                return await self._once(
                    listing_id=listing_id, session_id=None, message=message, approve=approve
                )
            except ClaudeSDKError as retry_err:
                logger.warning("agent run failed for listing %s: %s", listing_id, retry_err)
                return AgentReply(
                    text=f"⚠️ Der Assistent ist gerade gescheitert ({retry_err}).", ok=False
                )

    async def _once(
        self, *, listing_id: int, session_id: str | None, message: str, approve: Approve
    ) -> AgentReply:
        messages: list[Message] = []
        async for item in self._run(
            prompt=message,
            options=self._options(listing_id=listing_id, session_id=session_id, approve=approve),
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
