"""Application settings, loaded from environment variables / .env.

Secrets and model settings have no defaults: they must come from the environment,
so nothing sensitive lives in the code.
"""

from functools import lru_cache
from typing import Literal, Optional

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ── App ──
    app_name: str = "Paperless Cheque OCR API"
    app_version: str = "1.0.0"
    app_env: Literal["local", "uat", "prod"] = "local"
    api_v1_prefix: str = "/api/v1"
    docs_enabled: bool = True
    log_level: str = "INFO"
    logs_dir: str = "logs"
    cors_origins: list[str] = ["*"]

    # ── OCR model (required, from environment only) ──
    gemini_api_key: SecretStr
    gemini_model: str

    # ── OCR model: sampling ──
    gemini_temperature: float = Field(0.0, ge=0.0, le=2.0)
    gemini_top_p: Optional[float] = Field(0.95, ge=0.0, le=1.0)
    gemini_top_k: Optional[int] = Field(40, ge=1)
    gemini_max_output_tokens: int = Field(4096, ge=1)
    gemini_seed: Optional[int] = None

    # ── OCR model: thinking ──
    gemini_thinking_budget: Optional[int] = Field(0, ge=-1)
    gemini_thinking_level: Optional[Literal["low", "high"]] = None

    # ── OCR model: reliability ──
    gemini_timeout_seconds: int = Field(120, ge=1)
    gemini_max_retries: int = Field(2, ge=0)

    # ── Uploads ──
    max_files_per_request: int = Field(20, ge=1)
    max_file_size_mb: int = Field(15, ge=1)
    max_concurrent_requests: int = Field(5, ge=1)
    allowed_mime_types: frozenset[str] = frozenset({
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/heic",
        "image/heif",
        "application/pdf",
    })

    # ── File URLs (/cheques/match) ──
    # Hosts the API may download from; ".x.com" matches subdomains, [] allows any host
    allowed_file_hosts: list[str] = [".blob.core.windows.net"]
    download_timeout_seconds: int = Field(60, ge=1)
    # Local testing only: plain file names inside this folder. Ignored unless APP_ENV=local
    local_files_dir: Optional[str] = None

    @field_validator(
        "gemini_top_p", "gemini_top_k", "gemini_seed",
        "gemini_thinking_budget", "gemini_thinking_level", "local_files_dir",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, v):
        """Treat empty values in .env (e.g. `GEMINI_SEED=`) as unset."""
        return None if v == "" else v

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def local_files_enabled(self) -> bool:
        return self.app_env == "local" and bool(self.local_files_dir)


@lru_cache
def get_settings() -> Settings:
    return Settings()
