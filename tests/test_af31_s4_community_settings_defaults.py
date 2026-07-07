"""AF31-S4 - Community-owned settings defaults."""

from __future__ import annotations

from okto_pulse.community.adapters.composition import build_community_embedding
from okto_pulse.community.adapters.telemetry_effect_config import (
    COMMUNITY_DEFAULT_METRICS_BEACON_URL,
)
from okto_pulse.community.config import CommunitySettings


CANONICAL_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def test_af31_s4_community_derives_empty_and_legacy_upload_defaults(
    monkeypatch,
    tmp_path,
):
    for env_name in (
        "DATA_DIR",
        "DATABASE_URL",
        "UPLOAD_DIR",
        "METRICS_DIR",
        "KG_BASE_DIR",
        "KG_EMBEDDING_MODE",
        "KG_EMBEDDING_MODEL",
        "METRICS_BEACON_URL",
    ):
        monkeypatch.delenv(env_name, raising=False)

    data_dir = tmp_path / "pulse-home"
    expected_data_dir = data_dir.resolve()

    empty_upload = CommunitySettings(data_dir=str(data_dir), upload_dir="")
    legacy_upload = CommunitySettings(data_dir=str(data_dir), upload_dir="./uploads")

    for settings in (empty_upload, legacy_upload):
        assert settings.data_dir == str(expected_data_dir)
        assert settings.upload_dir == str(expected_data_dir / "uploads")
        assert settings.metrics_dir == str(expected_data_dir / "metrics")
        assert settings.kg_base_dir == str(expected_data_dir)
        assert settings.database_url == (
            f"sqlite+aiosqlite:///{expected_data_dir / 'data' / 'pulse.db'}"
        )
        assert settings.metrics_beacon_url == COMMUNITY_DEFAULT_METRICS_BEACON_URL
        assert settings.kg_embedding_mode == "sentence-transformers"
        assert settings.kg_embedding_model == CANONICAL_EMBEDDING_MODEL


def test_af31_s4_composition_fallback_model_is_canonical():
    class SettingsWithoutModel:
        kg_embedding_mode = "sentence-transformers"
        kg_embedding_dim = 384

    provider = build_community_embedding(settings=SettingsWithoutModel())

    assert provider.embedding_metadata()["model_name"] == CANONICAL_EMBEDDING_MODEL
