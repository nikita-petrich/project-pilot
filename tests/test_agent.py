"""The thread agent: option shape, session handling, and failure paths."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    Message,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
)

from project_pilot.agent import ALLOWED_TOOLS, MAX_TURNS, MCP_SERVER, ThreadAgent, describe

MCP_URL = "http://mcp:8765/mcp"
MCP_TOKEN = "tok"


def _context() -> ToolPermissionContext:
    return ToolPermissionContext(suggestions=[])


def _result(text: str | None = "Passt.", session: str = "sess-1") -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=1,
        session_id=session,
        stop_reason="end_turn",
        total_cost_usd=0.01,
        usage=None,
        result=text,
    )


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="claude-opus-5")


class _Runs:
    """A stand-in for the SDK's ``query``: records options, replays messages."""

    def __init__(self, *scripts: list[Message] | Exception) -> None:
        self._scripts = list(scripts)
        self.options: list[ClaudeAgentOptions] = []
        self.prompts: list[str] = []

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
        self.options.append(options)
        self.prompts.append(prompt)
        script = self._scripts.pop(0) if self._scripts else [_result()]

        async def stream() -> AsyncIterator[Message]:
            if isinstance(script, Exception):
                raise script
            for message in script:
                yield message

        return stream()


def _agent(runs: _Runs, workspace: Path) -> ThreadAgent:
    return ThreadAgent(
        api_key="sk-ant-test",
        mcp_url=MCP_URL,
        mcp_token=MCP_TOKEN,
        workspace=workspace,
        runner=runs,
    )


@pytest.mark.asyncio
async def test_options_carry_the_mcp_server_and_no_filesystem_settings(tmp_path: Path) -> None:
    runs = _Runs([_result()])

    await _agent(runs, tmp_path).reply(listing_id=42, session_id=None, message="Prüf das")

    options = runs.options[-1]
    assert options.mcp_servers == {
        MCP_SERVER: {
            "type": "http",
            "url": MCP_URL,
            # The token rides in a header; the client runs here, not at Anthropic.
            "headers": {"Authorization": f"Bearer {MCP_TOKEN}"},
        }
    }
    # The repo's own .claude/ holds the build workflow, not thread judgment.
    assert options.setting_sources == []
    assert options.cwd == tmp_path
    assert options.max_turns == MAX_TURNS
    assert options.env["ANTHROPIC_API_KEY"] == "sk-ant-test"


@pytest.mark.asyncio
async def test_shell_and_filesystem_are_not_taken_away(tmp_path: Path) -> None:
    """The whole point of the SDK here: the built-in tools stay available."""
    runs = _Runs([_result()])

    await _agent(runs, tmp_path).reply(listing_id=1, session_id=None, message="hi")

    options = runs.options[-1]
    # Available, but asked about — nothing is removed from the model's reach.
    assert options.permission_mode == "default"
    assert options.disallowed_tools == []
    assert options.tools is None
    assert options.can_use_tool is not None


@pytest.mark.asyncio
async def test_reading_is_pre_approved_and_writing_is_not(tmp_path: Path) -> None:
    runs = _Runs([_result()])

    await _agent(runs, tmp_path).reply(listing_id=1, session_id=None, message="hi")

    allowed = set(runs.options[-1].allowed_tools)
    assert {"Read", "Grep", "WebSearch"} <= allowed
    assert f"mcp__{MCP_SERVER}__project_pilot_check_listing" in allowed
    # The ones that write to disk, run a command, or send have to be asked about.
    for tool in ("Bash", "Write", "Edit", f"mcp__{MCP_SERVER}__project_pilot_send_application"):
        assert tool not in allowed
    assert set(ALLOWED_TOOLS) == allowed


@pytest.mark.asyncio
async def test_the_human_decides_each_call_that_is_not_pre_approved(tmp_path: Path) -> None:
    runs = _Runs([_result()])
    asked: list[tuple[str, str]] = []

    async def approve(tool: str, detail: str) -> bool:
        asked.append((tool, detail))
        return tool != "Bash"

    await _agent(runs, tmp_path).reply(listing_id=1, session_id=None, message="hi", approve=approve)
    gate = runs.options[-1].can_use_tool
    assert gate is not None

    allowed = await gate("Write", {"file_path": "/data/workspace/notes.md"}, _context())
    denied = await gate("Bash", {"command": "rm -rf /"}, _context())

    assert allowed.behavior == "allow"
    assert denied.behavior == "deny"
    assert asked == [("Write", "/data/workspace/notes.md"), ("Bash", "rm -rf /")]


def test_describe_names_what_the_call_would_do() -> None:
    assert describe("Bash", {"command": "ls -la"}) == "ls -la"
    assert describe("Write", {"file_path": "/tmp/x", "content": "…"}) == "/tmp/x"
    # Nothing recognisable still beats a bare tool name.
    assert "42" in describe("Other", {"whatever": 42})
    assert describe("Read", {}) == ""


def test_describe_clips_a_long_value() -> None:
    detail = describe("Bash", {"command": "echo " + "x" * 5_000})
    assert len(detail) < 400
    assert detail.endswith("…")


@pytest.mark.asyncio
async def test_system_prompt_appends_to_the_claude_code_preset(tmp_path: Path) -> None:
    runs = _Runs([_result()])

    await _agent(runs, tmp_path).reply(listing_id=77, session_id=None, message="hi")

    prompt: object = runs.options[-1].system_prompt
    assert isinstance(prompt, dict)
    assert prompt["preset"] == "claude_code"
    assert "Listing 77" in prompt["append"]


@pytest.mark.asyncio
async def test_a_known_session_is_resumed(tmp_path: Path) -> None:
    runs = _Runs([_result(session="sess-9")])

    reply = await _agent(runs, tmp_path).reply(
        listing_id=1, session_id="sess-9", message="und weiter?"
    )

    assert runs.options[-1].resume == "sess-9"
    assert reply.session_id == "sess-9"


@pytest.mark.asyncio
async def test_a_lost_session_starts_over_instead_of_failing(tmp_path: Path) -> None:
    runs = _Runs(CLIConnectionError("no such session"), [_result(session="sess-new")])

    reply = await _agent(runs, tmp_path).reply(
        listing_id=1, session_id="sess-gone", message="hallo"
    )

    assert [options.resume for options in runs.options] == ["sess-gone", None]
    assert reply.ok is True
    assert reply.session_id == "sess-new"


@pytest.mark.asyncio
async def test_a_failing_run_answers_instead_of_raising(tmp_path: Path) -> None:
    runs = _Runs(CLIConnectionError("boom"))

    reply = await _agent(runs, tmp_path).reply(listing_id=1, session_id=None, message="hallo")

    assert reply.ok is False
    assert "gescheitert" in reply.text


@pytest.mark.asyncio
async def test_prose_is_used_when_the_run_reports_no_result(tmp_path: Path) -> None:
    runs = _Runs([_assistant("Erste Hälfte."), _assistant("Zweite."), _result(text=None)])

    reply = await _agent(runs, tmp_path).reply(listing_id=1, session_id=None, message="hi")

    assert reply.text == "Erste Hälfte.\n\nZweite."
    assert reply.ok is True


@pytest.mark.asyncio
async def test_a_silent_run_is_reported_but_keeps_its_session(tmp_path: Path) -> None:
    runs = _Runs([_result(text=None, session="sess-2")])

    reply = await _agent(runs, tmp_path).reply(listing_id=1, session_id=None, message="hi")

    assert reply.ok is False
    assert reply.session_id == "sess-2"
