"""Endpoint-mode persistence and local control-plane coverage."""

from __future__ import annotations

import app.config as config
from app.config import AppSettings, LLMSettings
from app.web import create_app
from fastapi.testclient import TestClient

from tests.test_web import csrf_from


def test_endpoint_mode_defaults_to_completions_and_rejects_invalid_saved_values() -> None:
    assert LLMSettings().endpoint_mode == "completions"
    assert LLMSettings(endpoint_mode="RESPONSES").endpoint_mode == "responses"
    assert LLMSettings.from_mapping({"endpoint_mode": "base"}).endpoint_mode == "base"
    assert LLMSettings.from_mapping({"endpoint_mode": "not-an-endpoint"}).endpoint_mode == "completions"
    assert AppSettings.from_mapping({"llm": {"endpoint_mode": "invalid"}}).llm.endpoint_mode == "completions"


def test_settings_form_persists_the_selected_endpoint_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "keyring", None)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        token = csrf_from(client.get("/").text)
        response = client.post(
            "/settings",
            data={
                "csrf_token": token,
                "llm_endpoint_mode": "responses",
                "llm_base_url": "https://api.example.test/v1",
                "llm_model": "test-model",
                "timeout_seconds": "120",
                "global_reasoning_effort": "off",
                "media_budget_gib": "20",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        service = app.state.service
        assert service.settings.llm.endpoint_mode == "responses"
        assert service.public_settings()["llm_endpoint_mode"] == "responses"
        assert service.db.get_json_setting("app_settings", {})["llm"]["endpoint_mode"] == "responses"
        page = client.get("/")
        assert 'option value="responses" selected' in page.text
        assert "采用 OpenAI Responses 请求格式" in page.text


def test_settings_form_normalises_an_invalid_endpoint_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "keyring", None)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        token = csrf_from(client.get("/").text)
        response = client.post(
            "/settings",
            data={"csrf_token": token, "llm_endpoint_mode": "somewhere-else"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert app.state.service.settings.llm.endpoint_mode == "completions"
