"""The thread agent: request shape, tool restriction, history bounds, failures."""

import json
from typing import Any

import anthropic
import httpx2

from project_pilot.agent import HISTORY_TURNS, MCP_BETA, MCP_SERVER_NAME, ThreadAgent

MCP_URL = "https://mcp-project-pilot.example.io/t/tok/mcp"


class _Calls:
    """Captures what the SDK actually put on the wire."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []

    @property
    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(self.requests[-1].content)
        return body

    @property
    def headers(self) -> httpx2.Headers:
        return self.requests[-1].headers


def _agent(
    *, json_body: dict[str, object] | None = None, error: Exception | None = None
) -> tuple[ThreadAgent, _Calls]:
    calls = _Calls()

    def handle(request: httpx2.Request) -> httpx2.Response:
        calls.requests.append(request)
        if error is not None:
            raise error
        return httpx2.Response(200, json=json_body or _answer())

    client = anthropic.AsyncAnthropic(
        api_key="sk-ant-test",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
    )
    return ThreadAgent(
        api_key="sk-ant-test", mcp_url=MCP_URL, model="claude-opus-5", client=client
    ), calls


def _answer(text: str = "Passt.") -> dict[str, object]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


async def test_the_mcp_server_is_the_only_tool_source() -> None:
    # The security model of this feature: no bash, no filesystem, no web — the
    # agent can only reach project-pilot's own tools.
    agent, calls = _agent()
    await agent.reply(listing_id=42, history=[], message="passt das?")

    assert calls.payload["mcp_servers"] == [
        {"type": "url", "url": MCP_URL, "name": MCP_SERVER_NAME}
    ]
    assert calls.payload["tools"] == [{"type": "mcp_toolset", "mcp_server_name": MCP_SERVER_NAME}]
    # Both halves must name the same server, or the API rejects the request.
    assert calls.payload["mcp_servers"][0]["name"] == calls.payload["tools"][0]["mcp_server_name"]
    assert MCP_BETA in calls.headers["anthropic-beta"]


async def test_the_system_prompt_pins_the_thread_to_its_listing() -> None:
    agent, calls = _agent()
    await agent.reply(listing_id=534, history=[], message="und?")

    system = calls.payload["system"]
    assert "534" in system
    # Sending stays gated in the prompt, mirroring the MCP prompt's rule.
    assert "project_pilot_send_application" in system


async def test_history_is_sent_and_bounded() -> None:
    agent, calls = _agent()
    history = [{"role": "user", "text": f"m{i}"} for i in range(HISTORY_TURNS + 10)]
    await agent.reply(listing_id=1, history=history, message="jetzt")

    messages = calls.payload["messages"]
    assert len(messages) == HISTORY_TURNS + 1  # the bounded history plus this turn
    assert messages[-1] == {"role": "user", "content": "jetzt"}


async def test_assistant_turns_keep_their_role() -> None:
    agent, calls = _agent()
    history = [{"role": "user", "text": "a"}, {"role": "assistant", "text": "b"}]
    await agent.reply(listing_id=1, history=history, message="c")

    assert [m["role"] for m in calls.payload["messages"]] == ["user", "assistant", "user"]


async def test_reply_returns_the_text() -> None:
    agent, _ = _agent(json_body=_answer("Guter Match, 95 Punkte."))
    reply = await agent.reply(listing_id=1, history=[], message="?")
    assert reply.ok is True
    assert reply.text == "Guter Match, 95 Punkte."


async def test_an_api_failure_becomes_a_readable_sentence() -> None:
    # A silent gap in the thread would look like the bot ignored the message.
    agent, _ = _agent(error=httpx2.ConnectError("down"))
    reply = await agent.reply(listing_id=1, history=[], message="?")
    assert reply.ok is False
    assert "nicht erreichbar" in reply.text


async def test_a_turn_without_prose_still_says_something() -> None:
    agent, _ = _agent(json_body={**_answer(), "content": [{"type": "text", "text": "   "}]})
    reply = await agent.reply(listing_id=1, history=[], message="?")
    assert reply.ok is False
    assert reply.text.startswith("⚠️")
