from __future__ import annotations

from app.core.config import Settings
from app.services import llm


def test_legacy_picker_uses_the_deployed_default_model(monkeypatch, tmp_path) -> None:
    settings = Settings(
        chat_model="llama3.2:3b",
        default_model="qwen3-coder:30b",
        llm_config_path=str(tmp_path / "neo_llms.json"),
    )
    monkeypatch.setattr(llm, "get_settings", lambda: settings)

    configs, active_id = llm.LLMRegistry().list()

    assert active_id == "ollama-default"
    assert len(configs) == 1
    assert configs[0].model == "qwen3-coder:30b"
