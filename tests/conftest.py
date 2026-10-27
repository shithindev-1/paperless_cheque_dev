import pytest

from app.core.config import Settings


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
