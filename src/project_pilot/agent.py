"""The agent that answers inside a match topic.

One call to the Messages API per incoming message, with project-pilot's own MCP
server attached as the *only* tool source. That choice is the security model of
this feature: the agent can check a listing, draft, revise, set a recipient and
send — and it can do nothing else. There is no shell, no filesystem, no web.
The Claude Agent SDK would have brought those tools along and required
configuring them away; this way they never exist.

Anthropic's servers call the MCP server directly over HTTPS, so the bot process
never proxies tool traffic, and the conversation is plain text in Postgres
rather than a session file on one host.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anthropic

if TYPE_CHECKING:  # the SDK's request type; not needed at runtime
    from anthropic.types.beta import BetaMessageParam

logger = logging.getLogger(__name__)

MCP_SERVER_NAME = "project-pilot"
MCP_BETA = "mcp-client-2025-11-20"
# Turns kept per topic. Each turn re-reads the listing through the tools, so the
# history carries the conversation, not the facts — it can stay short.
HISTORY_TURNS = 20
MAX_TOKENS = 4_000

SYSTEM = """\
Du bist project-pilots Assistent im Telegram-Thread zu genau einem Projekt.

Dieser Thread gehört zu **Listing {listing_id}**. Wenn keine andere Nummer
genannt wird, ist das die gemeinte.

Deine Werkzeuge sind die `project_pilot_*`-Tools. Sie halten Nik's Profil, die
Urteilsregeln und den Bewerbungs-Stil — arbeite durch sie, nicht aus dem
Gedächtnis, und erfinde keine Fakten über Nik oder das Projekt.

Übliche Abläufe:
- Prüfen: `project_pilot_check_listing`. Deren Urteil gilt; überstimme es nicht
  mit deiner eigenen Lesart der Ausschreibung.
- Entwerfen: `project_pilot_draft_application`, Änderungen über
  `project_pilot_revise_application`.
- Kontaktdaten: `project_pilot_enrich_company`.
- Empfänger: `project_pilot_set_recipient`.

**Senden ist die einzige unumkehrbare Handlung.** Rufe
`project_pilot_send_application` erst, wenn du Empfänger und Betreff gezeigt und
Nik in *dieser* Unterhaltung ausdrücklich zugestimmt hat. Ein früheres „mach
mal" zählt nicht. Biete das Senden nicht von dir aus an.

Antworte auf Deutsch, knapp und ohne Floskeln. Du schreibst in Telegram: kein
Markdown, keine Überschriften, kurze Absätze. Nenne die `listing_id` oder
`application_id`, wenn sie für den nächsten Schritt gebraucht wird.
"""


@dataclass(frozen=True, slots=True)
class AgentReply:
    """What the agent produced, and whether it got there cleanly."""

    text: str
    ok: bool


class ThreadAgent:
    """Answers one message in one topic, using only the project-pilot MCP tools."""

    def __init__(
        self,
        *,
        api_key: str,
        mcp_url: str,
        model: str = "claude-opus-5",
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        # `client` exists so tests can hand in one on a mock transport; the SDK
        # ships its own HTTP stack, so there is nothing else to intercept.
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key)
        self._mcp_url = mcp_url
        self._model = model

    async def reply(
        self, *, listing_id: int, history: list[dict[str, str]], message: str
    ) -> AgentReply:
        """One turn: past turns plus this message in, the answer out.

        Never raises: a failing model call has to reach the user as a sentence in
        the thread, not as a silent gap.
        """
        messages: list[BetaMessageParam] = [
            {
                "role": "assistant" if turn["role"] == "assistant" else "user",
                "content": turn["text"],
            }
            for turn in history[-HISTORY_TURNS:]
            if turn.get("text")
        ]
        messages.append({"role": "user", "content": message})
        try:
            response = await self._client.beta.messages.create(
                model=self._model,
                max_tokens=MAX_TOKENS,
                betas=[MCP_BETA],
                system=SYSTEM.format(listing_id=listing_id),
                # Both halves are required: the server declaration alone is a
                # validation error, the toolset alone has nothing to point at.
                mcp_servers=[{"type": "url", "url": self._mcp_url, "name": MCP_SERVER_NAME}],
                tools=[{"type": "mcp_toolset", "mcp_server_name": MCP_SERVER_NAME}],
                messages=messages,
            )
        except anthropic.APIError as err:
            logger.warning("agent call failed for listing %s: %s", listing_id, err)
            return AgentReply(
                text=f"⚠️ Der Assistent ist gerade nicht erreichbar ({err}).", ok=False
            )

        text = "\n\n".join(
            block.text for block in response.content if block.type == "text" and block.text
        ).strip()
        if not text:
            # A turn that ends without prose (only tool blocks) would look like
            # the bot ignored the message.
            return AgentReply(
                text="⚠️ Der Assistent hat nichts geantwortet. Frag noch einmal.", ok=False
            )
        return AgentReply(text=text, ok=True)
