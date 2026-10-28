import json

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.errors import register_exception_handlers
from app.api.security import require_api_key
from app.core.config import Settings, get_settings


def _write(directory, name, data):
    (directory / name).write_text(json.dumps(data), encoding="utf-8")


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, gemini_api_key="k", **kwargs)


# ── config files ──

def test_env_file_overrides_shared_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENV", "uat")
    _write(tmp_path, "settings.json", {"uploads": {"max_file_size_mb": 10, "max_files_per_request": 5}})
    _write(tmp_path, "settings.uat.json", {"_comment": "x", "uploads": {"max_file_size_mb": 25},
                                            "ocr_model": {"gemini_model": "m-uat"}})
    s = _settings(api_keys="a")
    assert s.max_file_size_mb == 25          # env file wins
    assert s.max_files_per_request == 5      # shared default kept
    assert s.gemini_model == "m-uat"


def test_environment_variable_overrides_config_file(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("MAX_FILE_SIZE_MB", "7")
    _write(tmp_path, "settings.json", {"uploads": {"max_file_size_mb": 10}})
    assert _settings(gemini_model="m").max_file_size_mb == 7


@pytest.mark.parametrize("bad", [{"uploads": {"no_such_setting": 1}},
                                 {"secrets": {"gemini_api_key": "leak"}},
                                 {"app_env": "prod"}])
def test_config_file_rejects_unknown_keys_and_secrets(monkeypatch, tmp_path, bad):
    monkeypatch.setenv("APP_CONFIG_DIR", str(tmp_path))
    _write(tmp_path, "settings.json", bad)
    with pytest.raises(ValueError):
        _settings(gemini_model="m")


def test_api_keys_parsing_and_required_outside_local():
    assert [k.get_secret_value() for k in _settings(gemini_model="m", api_keys="a, b,").api_keys] == ["a", "b"]
    assert len(_settings(gemini_model="m", api_keys='["x","y"]').api_keys) == 2
    assert not _settings(gemini_model="m").auth_enabled
    with pytest.raises(ValidationError):
        _settings(gemini_model="m", app_env="uat")


# ── X-API-Key ──

def _client(settings: Settings) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/protected", dependencies=[Depends(require_api_key)])
    async def protected():
        return {"ok": True}

    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def test_api_key_required_when_configured():
    client = _client(_settings(gemini_model="m", api_keys="old-key,new-key"))
    assert client.get("/protected").status_code == 401
    assert client.get("/protected").json() == {"error": "Invalid or missing API key."}
    assert client.get("/protected", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/protected", headers={"X-API-Key": "new-key"}).status_code == 200
    assert client.get("/protected", headers={"X-API-Key": "old-key"}).status_code == 200


def test_api_key_not_required_when_none_configured_locally():
    assert _client(_settings(gemini_model="m")).get("/protected").status_code == 200
