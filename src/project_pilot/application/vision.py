"""Transcribe an uploaded screenshot via an OpenAI vision model.

Dropping a screenshot of a listing into the Slack channel is a normal way to ask for
a draft or a check, but neither the PDF nor the text path can read pixels. This is
the network boundary that turns image bytes into text; the port it satisfies lives
in ``documents.py`` so the extraction logic stays testable without a network call.
"""

import base64
import logging
from typing import TYPE_CHECKING

from openai import AsyncOpenAI

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam

logger = logging.getLogger(__name__)

# Verbatim transcription, not interpretation: the text feeds the same drafting and
# matching prompts as a pasted description, so summarizing here would lose detail.
VISION_PROMPT = (
    "Transcribe all text in this image in reading order. It is a screenshot of a "
    "freelance project listing. Keep the wording verbatim — do not translate, "
    "summarize, or add commentary. Reply with the transcription only, and reply with "
    "nothing at all if the image contains no readable text."
)


class OpenAiVisionClient:
    """Thin adapter over the OpenAI SDK's image input (network, not unit-tested)."""

    def __init__(
        self, api_key: str, *, model: str, client: AsyncOpenAI | None = None
    ) -> None:  # pragma: no cover
        self._client = client or AsyncOpenAI(api_key=api_key)
        self._model = model

    async def read_image(self, *, data: bytes, mime_type: str) -> str:  # pragma: no cover
        encoded = base64.b64encode(data).decode("ascii")
        messages: list[ChatCompletionMessageParam] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                    },
                ],
            }
        ]
        completion = await self._client.chat.completions.create(
            model=self._model, messages=messages
        )
        usage = completion.usage
        if usage is not None:
            logger.info(
                "vision transcription: model=%s tokens_in=%d tokens_out=%d",
                self._model,
                usage.prompt_tokens,
                usage.completion_tokens,
            )
        return completion.choices[0].message.content or ""
