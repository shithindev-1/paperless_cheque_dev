"""Gemini API client (singleton)."""

from functools import lru_cache

from google import genai
from google.genai import types

from app.core.config import get_settings


@lru_cache
def get_gemini_client() -> genai.Client:
    settings = get_settings()
    return genai.Client(
        api_key=settings.gemini_api_key.get_secret_value(),
        http_options=types.HttpOptions(timeout=settings.gemini_timeout_seconds * 1000),
    )
