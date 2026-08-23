"""Community edition settings — local-first, SQLite, ~/.okto-pulse/."""

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import DotEnvSettingsSource
from okto_pulse.core import CoreSettings
from okto_pulse.community.adapters.embedding import (
    COMMUNITY_DEFAULT_EMBEDDING_DIM,
    COMMUNITY_DEFAULT_EMBEDDING_MODE,
    COMMUNITY_DEFAULT_EMBEDDING_MODEL,
)
from okto_pulse.community.adapters.telemetry_effect_config import (
    COMMUNITY_DEFAULT_METRICS_BEACON_URL,
)

GRAPH_DB_MAX_SIZE_GB_VALUES: tuple[int, ...] = (2, 4, 8, 16, 32, 64)
DataDirOrigin = Literal["explicit", "DATA_DIR", "OKTO_PULSE_HOME", "default"]


def validate_graph_db_max_size_gb(value: int) -> int:
    if value not in GRAPH_DB_MAX_SIZE_GB_VALUES:
        raise ValueError(
            "kg_kuzu_max_db_size_gb must be one of "
            "2, 4, 8, 16, 32, 64 GB (a power of 2)"
        )
    return value


class CommunitySettings(CoreSettings, BaseSettings):
    """Settings for the community edition (local-first, single-user)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    debug: bool = False
    environment: str = "development"
    host: str = "127.0.0.1"  # Community is local-only — bind to loopback
    port: int = 8100
    database_url: str = ""
    upload_dir: str = ""
    max_upload_size: int = 10 * 1024 * 1024
    data_dir: str = ""  # Default set in validator
    data_dir_origin: DataDirOrigin = Field(default="default", exclude=True)
    metrics_dir: str = ""
    metrics_beacon_url: str = COMMUNITY_DEFAULT_METRICS_BEACON_URL
    mcp_server_name: str = "okto-pulse"
    mcp_server_version: str = "0.3.3"
    mcp_port: int = 8101
    # MCP and API/UI share one event loop.  Keep tool-call bursts bounded while
    # leaving transport sessions, streams and every REST route outside the gate.
    mcp_admission_max_active: int = Field(4, ge=1, le=64)
    mcp_admission_max_active_per_session: int = Field(2, ge=1, le=64)
    # Embedded persistence remains single-writer.  This is intentionally a
    # validated constant rather than a tuning escape hatch.
    mcp_admission_max_active_writers: int = Field(1, ge=1, le=1)
    mcp_admission_max_queued: int = Field(16, ge=0, le=256)
    mcp_admission_max_queued_per_session: int = Field(4, ge=0, le=256)
    mcp_admission_wait_timeout_ms: int = Field(250, ge=0, le=10_000)
    mcp_admission_retry_after_ms: int = Field(500, ge=1, le=60_000)
    cors_origins: str = "*"
    kg_base_dir: str = "~/.okto-pulse"

    # Community ships sentence-transformers as a mandatory dep (pyproject.toml),
    # so override the core default of "stub" — semantic KG search needs real
    # embeddings out of the box. Users can still flip to "stub" via env.
    kg_embedding_mode: str = COMMUNITY_DEFAULT_EMBEDDING_MODE
    kg_embedding_model: str = COMMUNITY_DEFAULT_EMBEDDING_MODEL
    kg_embedding_dim: int = COMMUNITY_DEFAULT_EMBEDDING_DIM
    # Each open Ladybug Database owns its own native buffer pool.  Conservative
    # defaults keep a local multi-board process below the former 4 x 512 MB
    # baseline while persisted/operator overrides remain backwards compatible.
    kg_kuzu_buffer_pool_mb: int = Field(256, ge=128, le=512)
    # Global Discovery is a separate Database and does not need the full board
    # write budget.  It is intentionally environment/config-only for now; the
    # legacy runtime-settings API continues to govern the board pool unchanged.
    kg_global_kuzu_buffer_pool_mb: int = Field(128, ge=128, le=512)
    kg_kuzu_max_db_size_gb: int = Field(2, ge=2, le=64)
    kg_connection_pool_size: int = Field(2, ge=1, le=32)
    kg_wal_salvage_enabled: bool = True
    kg_wal_only_recovery_enabled: bool = True
    kg_decay_tick_batch_size: int = 200
    kg_write_barrier_mode: str = "soft"
    mcp_legacy_coverage: bool = Field(
        False,
        validation_alias="OKTO_PULSE_LEGACY_COVERAGE",
    )
    mcp_legacy_offset: bool = Field(
        False,
        validation_alias="OKTO_PULSE_LEGACY_OFFSET",
    )

    def __init__(self, **values: object) -> None:
        """Resolve the data-home path and its provenance as one identity.

        The legacy ``OKTO_PULSE_HOME`` environment variable intentionally
        outranks ``DATA_DIR`` loaded from dotenv. Resolve that cross-source
        precedence before BaseSettings merges its sources, then pass the path
        and provenance together as authoritative init values. Empty and
        whitespace-only candidates are absent, and ``DATA_DIR_ORIGIN`` can
        never spoof the result.
        """

        prepared = dict(values)
        supplied_data_dir = str(prepared.get("data_dir") or "").strip()
        environment_data_dir = (os.environ.get("DATA_DIR") or "").strip()
        legacy_environment_home = (os.environ.get("OKTO_PULSE_HOME") or "").strip()

        env_file = prepared.get("_env_file", self.model_config.get("env_file"))
        env_file_encoding = prepared.get(
            "_env_file_encoding",
            self.model_config.get("env_file_encoding"),
        )
        dotenv_values = (
            DotEnvSettingsSource(
                type(self),
                env_file=env_file,
                env_file_encoding=env_file_encoding,
            )()
            if env_file is not None
            else {}
        )
        dotenv_data_dir = str(dotenv_values.get("data_dir") or "").strip()

        if supplied_data_dir:
            origin: DataDirOrigin = "explicit"
            resolved_data_dir = supplied_data_dir
        elif environment_data_dir:
            origin = "DATA_DIR"
            resolved_data_dir = environment_data_dir
        elif legacy_environment_home:
            origin = "OKTO_PULSE_HOME"
            resolved_data_dir = legacy_environment_home
        elif dotenv_data_dir:
            origin = "DATA_DIR"
            resolved_data_dir = dotenv_data_dir
        else:
            origin = "default"
            resolved_data_dir = str(Path.home() / ".okto-pulse")

        prepared["data_dir"] = resolved_data_dir
        prepared["data_dir_origin"] = origin
        super().__init__(**prepared)

    @field_validator("kg_kuzu_max_db_size_gb")
    @classmethod
    def _validate_graph_db_max_size_gb(cls, value: int) -> int:
        return validate_graph_db_max_size_gb(value)

    @property
    def cors_origins_list(self) -> list[str]:
        return [
            origin.strip() for origin in self.cors_origins.split(",") if origin.strip()
        ]

    @model_validator(mode="after")
    def _derive_paths(self) -> "CommunitySettings":
        if self.mcp_admission_max_active_per_session > self.mcp_admission_max_active:
            raise ValueError(
                "mcp_admission_max_active_per_session cannot exceed "
                "mcp_admission_max_active"
            )
        if self.mcp_admission_max_queued_per_session > self.mcp_admission_max_queued:
            raise ValueError(
                "mcp_admission_max_queued_per_session cannot exceed "
                "mcp_admission_max_queued"
            )
        if not self.data_dir:
            self.data_dir = os.environ.get("OKTO_PULSE_HOME") or str(
                Path.home() / ".okto-pulse"
            )
        data_path = Path(self.data_dir).expanduser().resolve()
        self.data_dir = str(data_path)
        # Only override if still unset or at the legacy core default value.
        if (
            not self.database_url
            or self.database_url == "sqlite+aiosqlite:///./dashboard.db"
        ):
            db_path = data_path / "data" / "pulse.db"
            self.database_url = f"sqlite+aiosqlite:///{db_path}"
        if not self.upload_dir or self.upload_dir == "./uploads":
            self.upload_dir = str(data_path / "uploads")
        if not self.metrics_dir:
            self.metrics_dir = str(data_path / "metrics")
        default_kg_base = "~/.okto-pulse"
        if not self.kg_base_dir or self.kg_base_dir == default_kg_base:
            self.kg_base_dir = str(data_path)
        else:
            self.kg_base_dir = str(Path(self.kg_base_dir).expanduser().resolve())
        # Community edition is local-only — allow all origins to avoid CORS
        # issues regardless of which port the user configures via CLI
        self.cors_origins = "*"
        return self
