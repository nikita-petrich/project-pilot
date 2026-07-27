"""Tests for the shared RobotsGate (pure logic with a fake robots getter)."""

from project_pilot.enrichment.robots import RobotsGate


class _FakeGetter:
    def __init__(self, text: str | None) -> None:
        self.text = text
        self.calls: list[str] = []

    async def __call__(self, url: str) -> str | None:
        self.calls.append(url)
        return self.text


async def test_gate_allows_and_denies_per_rules() -> None:
    gate = RobotsGate("ua", _FakeGetter("User-agent: *\nDisallow: /private"))
    assert await gate.allowed("https://firma.de/impressum") is True
    assert await gate.allowed("https://firma.de/private/data") is False


async def test_gate_caches_per_host() -> None:
    getter = _FakeGetter("User-agent: *\nAllow: /")
    gate = RobotsGate("ua", getter)
    await gate.allowed("https://firma.de/a")
    await gate.allowed("https://firma.de/b")
    assert getter.calls == ["https://firma.de/robots.txt"]  # fetched once, then cached


async def test_gate_fails_open_when_robots_unreachable() -> None:
    gate = RobotsGate("ua", _FakeGetter(None))
    assert await gate.allowed("https://firma.de/anything") is True
