"""Generation config (temperature, top_p, top_k, thinking...) and the model call."""

import asyncio
import logging
from typing import Optional, TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from app.core.config import Settings
from app.core.tracing import current_trace
from app.schemas import TokenUsage

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def build_thinking_config(settings: Settings) -> Optional[types.ThinkingConfig]:
    """Gemini 3+ uses thinking_level; Gemini 2.5 uses thinking_budget."""
    if settings.gemini_thinking_level:
        return types.ThinkingConfig(thinking_level=settings.gemini_thinking_level.upper())
    if settings.gemini_thinking_budget is not None:
        return types.ThinkingConfig(thinking_budget=settings.gemini_thinking_budget)
    return None


def build_generation_config(
    settings: Settings,
    system_instruction: str,
    response_schema: type[BaseModel],
) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=settings.gemini_temperature,
        top_p=settings.gemini_top_p,
        top_k=settings.gemini_top_k,
        max_output_tokens=settings.gemini_max_output_tokens,
        seed=settings.gemini_seed,
        thinking_config=build_thinking_config(settings),
        response_mime_type="application/json",
        response_schema=response_schema,
    )


def extract_usage(response: types.GenerateContentResponse) -> TokenUsage:
    meta = response.usage_metadata
    if meta is None:
        return TokenUsage()
    return TokenUsage(
        prompt_tokens=meta.prompt_token_count or 0,
        output_tokens=meta.candidates_token_count or 0,
        thinking_tokens=meta.thoughts_token_count or 0,
        total_tokens=meta.total_token_count or 0,
    )


async def generate_structured(
    client: genai.Client,
    model: str,
    contents: list,
    config: types.GenerateContentConfig,
    response_model: type[T],
    max_retries: int = 2,
    label: str = "",
) -> tuple[T, TokenUsage]:
    """Call Gemini, parse the JSON response into `response_model`, and return token usage.

    Retries with exponential backoff on rate limits and transient server errors, and
    immediately when the model returns cut-off or invalid JSON (e.g. a repetition loop).
    """
    attempt = 0
    while True:
        usage, text = None, None
        try:
            response = await client.aio.models.generate_content(
                model=model, contents=contents, config=config
            )
            usage = extract_usage(response)
            if isinstance(response.parsed, response_model):
                return response.parsed, usage
            text = response.text
            if not text:
                reason = response.candidates[0].finish_reason if response.candidates else "unknown"
                raise ValueError(f"Empty response from model (finish_reason={reason})")
            return response_model.model_validate_json(text), usage

        except errors.APIError as exc:
            if exc.code not in RETRYABLE_STATUS_CODES or attempt >= max_retries:
                raise
            delay = 2 ** attempt
            logger.warning("Gemini error %s, retrying in %ss (attempt %s/%s)",
                           exc.code, delay, attempt + 1, max_retries)
            _trace_retry(label, attempt + 1, f"API error {exc.code}", None, None)
            await asyncio.sleep(delay)
            attempt += 1

        except ValueError as exc:  # pydantic ValidationError included: empty / cut-off / bad JSON
            if attempt >= max_retries:
                raise
            logger.warning("Invalid model output (%s), retrying (attempt %s/%s)",
                           str(exc).splitlines()[0][:200], attempt + 1, max_retries)
            # Tokens of the failed attempt are still billed: keep them in the trace
            _trace_retry(label, attempt + 1, str(exc).splitlines()[0][:200], usage, text)
            attempt += 1


def _trace_retry(label: str, attempt: int, reason: str, usage: Optional[TokenUsage],
                 text: Optional[str]) -> None:
    trace = current_trace()
    if trace:
        # Start and end of a broken answer show which field it got stuck on
        trace.event("model_retry", file=label, attempt=attempt, reason=reason,
                    wasted_tokens=usage.total_tokens if usage else None,
                    output_start=text[:160] if text else None,
                    output_end=text[-60:] if text and len(text) > 160 else None)
