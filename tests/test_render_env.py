"""Tests for the deploy-time .env renderer (deploy/render-env.py).

The script gates every deploy, and its failure mode is silent: a rendered file with
the wrong LLM key yields a container that starts healthy and scores every listing
`llm_error`. It lives outside the package (hyphenated script name), so it is loaded
from its path here.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "render-env.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("render_env", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_env"] = module
    spec.loader.exec_module(module)
    return module


render_env = _load_script()


def _complete(**overrides: str) -> dict[str, str]:
    """A settings blob that renders cleanly, before the test breaks one thing."""
    settings = {
        "LLM_MODEL": "gpt-mini",
        "OPENAI_API_KEY": "sk-x",
        "SEARCH_URLS": "https://www.freelancermap.de/x",
        "TELEGRAM_BOT_TOKEN": "123456789:" + "A" * 35,
        "TELEGRAM_CHAT_ID": "12345",
        "MCP_TOKEN": "t",
        "PROXY_NETWORK": "edge",
    }
    settings.update(overrides)
    return {key: value for key, value in settings.items() if value}


def test_a_complete_openai_environment_renders() -> None:
    assert render_env.problems(_complete()) == []


def test_openai_is_the_default_provider() -> None:
    """No LLM_PROVIDER set must keep meaning OpenAI, so existing deploys don't break."""
    assert render_env.problems(_complete(OPENAI_API_KEY="")) == [
        "OPENAI_API_KEY is not set, and LLM_PROVIDER is 'openai'"
    ]


def test_anthropic_requires_its_own_key() -> None:
    """An OpenAI key present is exactly the trap: it must not satisfy the check."""
    problems = render_env.problems(_complete(LLM_PROVIDER="anthropic"))

    assert problems == ["ANTHROPIC_API_KEY is not set, and LLM_PROVIDER is 'anthropic'"]


def test_anthropic_renders_with_its_key() -> None:
    settings = _complete(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-ant-x")

    assert render_env.problems(settings) == []


def test_both_keys_may_be_present_so_switching_back_is_one_line() -> None:
    settings = _complete(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-ant-x")

    assert render_env.problems(settings) == []
    assert render_env.problems({**settings, "LLM_PROVIDER": "openai"}) == []


def test_an_unknown_provider_is_rejected() -> None:
    settings = _complete(LLM_PROVIDER="mistral", ANTHROPIC_API_KEY="sk-ant-x")

    assert render_env.problems(settings) == [
        "LLM_PROVIDER is 'mistral'; expected one of ['anthropic', 'openai']"
    ]


def test_provider_casing_is_normalized_like_the_app_does() -> None:
    settings = _complete(LLM_PROVIDER="Anthropic", ANTHROPIC_API_KEY="sk-ant-x")

    assert render_env.problems(settings) == []


def test_a_padded_provider_is_caught_by_the_generic_whitespace_check() -> None:
    """The renderer is stricter than Settings: a .env cannot carry the padding safely."""
    settings = _complete(LLM_PROVIDER="anthropic ", ANTHROPIC_API_KEY="sk-ant-x")

    assert "LLM_PROVIDER has leading or trailing whitespace" in render_env.problems(settings)
