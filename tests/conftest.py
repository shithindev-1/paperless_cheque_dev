import pytest

from app.core.config import Settings


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path_factory):
    """Keep the developer's .env / config files and environment out of tests."""
    monkeypatch.setenv("APP_CONFIG_DIR", str(tmp_path_factory.mktemp("config")))
    for name in ("APP_ENV", "API_KEYS", "GEMINI_API_KEY", "GEMINI_MODEL",
                 "AZURE_STORAGE_CONNECTION_STRING", "AZURE_BLOB_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Settings for tests: dummy credentials, no .env file, local files in tmp_path."""
    return Settings(
        _env_file=None,
        gemini_api_key="test-key",
        gemini_model="test-model",
        app_env="local",
        local_files_dir=str(tmp_path),
    )
