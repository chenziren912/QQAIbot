"""Persistence and migration coverage for the local API-key JSON file."""

from __future__ import annotations

import json
import hashlib

import app.config as config
from app.config import DEFAULT_PROMPT, LLMSettings, SecretStore
from app.web import create_app
from fastapi.testclient import TestClient


def test_llm_api_key_persists_in_local_json_without_exposing_a_keyring_dependency(tmp_path) -> None:
    key_file = tmp_path / "api-key.json"
    store = SecretStore(key_file)

    store.set_llm_api_key("test-local-api-key")

    assert json.loads(key_file.read_text(encoding="utf-8")) == {
        "version": 1,
        "api_key": "test-local-api-key",
    }
    # A fresh process/store instance reads the durable local JSON value.
    assert SecretStore(key_file).get_llm_api_key() == "test-local-api-key"


def test_existing_windows_credential_api_key_migrates_once_to_local_json(tmp_path, monkeypatch) -> None:
    class LegacyKeyring:
        def __init__(self) -> None:
            self.values = {(SecretStore.SERVICE_NAME, SecretStore.API_KEY_NAME): "legacy-api-key"}
            self.deleted = []

        def get_password(self, service: str, name: str):
            return self.values.get((service, name))

        def delete_password(self, service: str, name: str) -> None:
            self.deleted.append((service, name))
            self.values.pop((service, name), None)

    legacy = LegacyKeyring()
    monkeypatch.setattr(config, "keyring", legacy)
    key_file = tmp_path / "api-key.json"

    assert SecretStore(key_file, migrate_legacy_api_key=True).get_llm_api_key() == "legacy-api-key"
    assert json.loads(key_file.read_text(encoding="utf-8"))["api_key"] == "legacy-api-key"
    assert legacy.deleted == [(SecretStore.SERVICE_NAME, SecretStore.API_KEY_NAME)]


def test_corrupt_json_does_not_overwrite_or_resurrect_a_legacy_key(tmp_path, monkeypatch) -> None:
    class LegacyKeyring:
        def get_password(self, service: str, name: str) -> str:
            return "legacy-api-key"

    monkeypatch.setattr(config, "keyring", LegacyKeyring())
    key_file = tmp_path / "api-key.json"
    key_file.write_text("{ not JSON", encoding="utf-8")

    store = SecretStore(key_file, migrate_legacy_api_key=True)
    assert store.get_llm_api_key() is None
    assert key_file.read_text(encoding="utf-8") == "{ not JSON"
    assert "无法读取本地 API Key JSON" in store.api_key_warning


def test_control_plane_never_renders_the_json_api_key(tmp_path) -> None:
    app = create_app(tmp_path)
    with TestClient(app) as client:
        app.state.service.secret_store.set_llm_api_key("do-not-render-this-key")
        page = client.get("/")

    assert page.status_code == 200
    assert "do-not-render-this-key" not in page.text


def test_default_app_data_dir_uses_localappdata_even_when_project_is_elsewhere(
    tmp_path, monkeypatch
) -> None:
    local_app_data = tmp_path / "用户 数据"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("QQ_AI_DATA_DIR", raising=False)

    app = create_app()
    try:
        assert app.state.service.data_dir == (
            local_app_data / "QQAIGroupAgent" / "data"
        ).resolve()
        assert app.state.service.db.path.parent == app.state.service.data_dir
    finally:
        app.state.service.db.close()


def test_only_the_exact_legacy_default_prompt_is_migrated(monkeypatch) -> None:
    legacy_default = "旧版默认提示词：所有结构化答案都必须渲染。"
    monkeypatch.setattr(
        config,
        "_LEGACY_MANDATORY_RENDER_DEFAULT_PROMPT_SHA256",
        hashlib.sha256(legacy_default.encode("utf-8")).hexdigest(),
    )

    assert LLMSettings(global_prompt=legacy_default).global_prompt == DEFAULT_PROMPT
    assert LLMSettings(global_prompt=legacy_default + " 自定义补充").global_prompt == (
        legacy_default + " 自定义补充"
    )
