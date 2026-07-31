"""Pydantic schemas for evaluation: the structured LLM MatchVerdict."""

from typing import Literal, Self

from pydantic import BaseModel


class MatchVerdict(BaseModel):
    """Structured output of the stage-3 LLM match.

    All fields are required and there are no numeric range or default constraints,
    so the model round-trips cleanly through OpenAI strict structured outputs.
    ``project_title`` is what a pasted description gets rendered under when the text
    carries no headline of its own.
    """

    project_title: str
    verdict: Literal["match", "no_match"]
    score: int
    reasons: list[str]
    matching_skills: list[str]
    missing_requirements: list[str]
    risk_flags: list[str]

    @classmethod
    def llm_error_fallback(cls, detail: str = "") -> Self:
        """The safe fallback when the LLM fails or returns an invalid schema."""
        reasons = ["llm_error"]
        if detail:
            reasons.append(detail)
        return cls(
            project_title="",
            verdict="no_match",
            score=0,
            reasons=reasons,
            matching_skills=[],
            missing_requirements=[],
            risk_flags=[],
        )
