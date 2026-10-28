"""Application settings.

Sources, highest priority first:
    1. environment variables
    2. .env                          secrets only (API keys, connection strings) + APP_ENV
    3. config/settings.<APP_ENV>.json per-environment values (not committed)
    4. config/settings.json           shared defaults (committed)
    5. defaults in this class

Secrets and the model name have no defaults, so nothing sensitive lives in the code.
"""

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

from dotenv import dotenv_values
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
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

    # ── Client authentication (secret, .env only) ──
    # Keys accepted in the X-API-Key header; several allow rotation. Required outside local.
    api_keys: Annotated[list[SecretStr], NoDecode] = []

    # ── OCR model (key from .env; model name from settings.<env>.json) ──
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

    # ── Azure Blob Storage (connection string from .env; base URL from settings.<env>.json) ──
    # URLs under AZURE_BLOB_BASE_URL are read with the connection string, and
    # processed (cropped / rotated) cheques are saved next to the original.
    azure_storage_connection_string: Optional[SecretStr] = None
    azure_blob_base_url: Optional[str] = None
    # Hours a read-only SAS token on returned URLs stays valid; 0 = plain URL (public container)
    azure_sas_expiry_hours: int = Field(0, ge=0)

    # ── Cheque image processing (/cheques/match) ──
    image_processing_enabled: bool = True
    processed_file_suffix: str = "_ocr"
    crop_padding_percent: float = Field(2.0, ge=0.0, le=20.0)
    pdf_render_dpi: int = Field(200, ge=72, le=600)
    processed_jpeg_quality: int = Field(92, ge=50, le=100)

    @field_validator(
        "gemini_top_p", "gemini_top_k", "gemini_seed",
        "gemini_thinking_budget", "gemini_thinking_level", "local_files_dir",
        "azure_storage_connection_string", "azure_blob_base_url",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, v):
        """Treat empty values in .env (e.g. `GEMINI_SEED=`) as unset."""
        return None if v == "" else v

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_api_keys(cls, v):
        """Accept a JSON list or a comma-separated string; drop blanks."""
        if isinstance(v, str):
            v = json.loads(v) if v.strip().startswith("[") else v.split(",")
        return [k.strip() for k in v if str(k).strip()] if v else []

    @model_validator(mode="after")
    def _require_api_keys_outside_local(self):
        if self.app_env != "local" and not self.api_keys:
            raise ValueError("API_KEYS must be set when APP_ENV is not 'local'.")
        return self

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        json_settings = JsonConfigSettingsSource(settings_cls, dotenv_settings.env_file)
        return init_settings, env_settings, dotenv_settings, json_settings, file_secret_settings

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_keys)

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def local_files_enabled(self) -> bool:
        return self.app_env == "local" and bool(self.local_files_dir)

    @property
    def blob_storage_enabled(self) -> bool:
        return bool(self.azure_storage_connection_string and self.azure_blob_base_url)


class JsonConfigSettingsSource(PydanticBaseSettingsSource):
    """Reads config/settings.json, then config/settings.<APP_ENV>.json on top.

    Files are grouped in sections ({"uploads": {"max_file_size_mb": 15}}); keys are the
    setting names. Keys starting with "_" are comments. Unknown keys are an error.
    """

    def __init__(self, settings_cls: type[BaseSettings], env_file: Any) -> None:
        super().__init__(settings_cls)
        config_dir = Path(os.environ.get("APP_CONFIG_DIR") or PROJECT_ROOT / "config")
        app_env = _resolve_app_env(env_file)
        self._values: dict[str, Any] = {}
        for path in (config_dir / "settings.json", config_dir / f"settings.{app_env}.json"):
            if path.is_file():
                self._values.update(_flatten(json.loads(path.read_text(encoding="utf-8")), path,
                                             settings_cls.model_fields))

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._values.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._values)


def _resolve_app_env(env_file: Any) -> str:
    """APP_ENV from the environment, else from .env, else 'local'."""
    if os.environ.get("APP_ENV"):
        return os.environ["APP_ENV"]
    if env_file and Path(env_file).is_file():
        return dotenv_values(env_file).get("APP_ENV") or "local"
    return "local"


def _flatten(data: dict, path: Path, fields: dict) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, value in data.items():
        if key.startswith("_"):
            continue
        items = value.items() if isinstance(value, dict) and key not in fields else [(key, value)]
        for name, item in items:
            if name.startswith("_"):
                continue
            if name not in fields or name in ("app_env", "api_keys", "gemini_api_key",
                                              "azure_storage_connection_string"):
                raise ValueError(f"{path.name}: '{name}' is not allowed in a config file.")
            values[name] = item
    return values


@lru_cache
def get_settings() -> Settings:
    return Settings()
