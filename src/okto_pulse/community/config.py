"""Community edition settings — local-first, SQLite, ~/.okto-pulse/."""

import os
from pathlib import Path
from typing import Literal

from okto_pulse.core import CoreSettings
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import DotEnvSettingsSource

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
GraphBackend = Literal["ladybug", "grafx"]
GrafxDescriptorRevalidation = Literal["strict", "generation"]

PULSE_GRAFX_DEFAULT_PAGE_SIZE = 8192
PULSE_GRAFX_MIN_PAGE_SIZE = 4096
PULSE_GRAFX_MAX_PAGE_SIZE = 32768

_LADYBUG_SETTING_ALIASES: tuple[tuple[str, str], ...] = (
    ("kg_ladybug_buffer_pool_mb", "kg_kuzu_buffer_pool_mb"),
    ("kg_global_ladybug_buffer_pool_mb", "kg_global_kuzu_buffer_pool_mb"),
    ("kg_ladybug_max_db_size_gb", "kg_kuzu_max_db_size_gb"),
)


class CommunitySettingsAliasConflict(ValueError):
    """A canonical Ladybug setting disagrees with its compatibility name."""

    code = "community_settings_alias_conflict"

    def __init__(self, canonical_name: str, legacy_name: str, *, source: str) -> None:
        self.canonical_name = canonical_name
        self.legacy_name = legacy_name
        self.source = source
        super().__init__(
            f"{canonical_name} conflicts with compatibility setting "
            f"{legacy_name} in {source}"
        )


def validate_grafx_page_size(value: int) -> int:
    """Validate the persisted page geometry required by the Pulse schema."""

    if type(value) is not int:
        raise ValueError("kg_grafx_page_size must be an integer")
    if not PULSE_GRAFX_MIN_PAGE_SIZE <= value <= PULSE_GRAFX_MAX_PAGE_SIZE:
        raise ValueError("kg_grafx_page_size must be between 4096 and 32768 bytes")
    if value & (value - 1):
        raise ValueError("kg_grafx_page_size must be a power of two")
    return value


def validate_grafx_descriptor_revalidation(value: object) -> str:
    """Validate the process-local descriptor policy accepted by Okto Grafx."""

    if type(value) is not str or value not in {"strict", "generation"}:
        raise ValueError(
            "kg_grafx_descriptor_revalidation must be 'strict' or 'generation'"
        )
    return value


def _equivalent_setting_values(left: object, right: object) -> bool:
    """Compare settings as integers while leaving invalid input to Pydantic."""

    try:
        return int(str(left).strip()) == int(str(right).strip())
    except (TypeError, ValueError):
        return left == right


def _reject_alias_conflicts(
    values: dict[str, object],
    *,
    environment: dict[str, object],
    dotenv: dict[str, object],
) -> None:
    """Reject ambiguous aliases within each settings-precedence source."""

    sources = (
        ("init", {str(key).casefold(): value for key, value in values.items()}),
        ("environment", environment),
        ("dotenv", dotenv),
    )
    for canonical_name, legacy_name in _LADYBUG_SETTING_ALIASES:
        for source_name, source_values in sources:
            canonical = canonical_name.casefold()
            legacy = legacy_name.casefold()
            if canonical not in source_values or legacy not in source_values:
                continue
            if not _equivalent_setting_values(
                source_values[canonical], source_values[legacy]
            ):
                raise CommunitySettingsAliasConflict(
                    canonical_name,
                    legacy_name,
                    source=source_name,
                )


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
    # Backend selection is explicit for both routing domains.  The defaults
    # preserve the installed Community behavior; a persisted per-scope binding
    # remains authoritative once the M-PULSE-6 router acquires it.
    kg_graph_backend: GraphBackend = "ladybug"
    kg_global_graph_backend: GraphBackend = "ladybug"
    kg_grafx_page_size: int = PULSE_GRAFX_DEFAULT_PAGE_SIZE
    kg_grafx_descriptor_revalidation: GrafxDescriptorRevalidation = "strict"

    # Community ships sentence-transformers as a mandatory dep (pyproject.toml),
    # so override the core default of "stub" — semantic KG search needs real
    # embeddings out of the box. Users can still flip to "stub" via env.
    kg_embedding_mode: str = COMMUNITY_DEFAULT_EMBEDDING_MODE
    kg_embedding_model: str = COMMUNITY_DEFAULT_EMBEDDING_MODEL
    kg_embedding_dim: int = COMMUNITY_DEFAULT_EMBEDDING_DIM
    # Each open Ladybug Database owns its own native buffer pool.  Conservative
    # defaults keep a local multi-board process below the former 4 x 512 MB
    # baseline while persisted/operator overrides remain backwards compatible.
    kg_kuzu_buffer_pool_mb: int = Field(
        256,
        ge=128,
        le=512,
        validation_alias=AliasChoices(
            "kg_kuzu_buffer_pool_mb",
            "kg_ladybug_buffer_pool_mb",
        ),
    )
    # Global Discovery is a separate Database and does not need the full board
    # write budget.  It is intentionally environment/config-only for now; the
    # legacy runtime-settings API continues to govern the board pool unchanged.
    kg_global_kuzu_buffer_pool_mb: int = Field(
        128,
        ge=128,
        le=512,
        validation_alias=AliasChoices(
            "kg_global_kuzu_buffer_pool_mb",
            "kg_global_ladybug_buffer_pool_mb",
        ),
    )
    kg_kuzu_max_db_size_gb: int = Field(
        2,
        ge=2,
        le=64,
        validation_alias=AliasChoices(
            "kg_kuzu_max_db_size_gb",
            "kg_ladybug_max_db_size_gb",
        ),
    )
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
        dotenv_source = (
            DotEnvSettingsSource(
                type(self),
                env_file=env_file,
                env_file_encoding=env_file_encoding,
            )
            if env_file is not None
            else None
        )
        dotenv_values = dotenv_source() if dotenv_source is not None else {}
        raw_dotenv_values = (
            dict(getattr(dotenv_source, "env_vars", {}))
            if dotenv_source is not None
            else {}
        )
        _reject_alias_conflicts(
            prepared,
            environment={key.casefold(): value for key, value in os.environ.items()},
            dotenv={key.casefold(): value for key, value in raw_dotenv_values.items()},
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

    @field_validator("kg_grafx_page_size")
    @classmethod
    def _validate_grafx_page_size(cls, value: int) -> int:
        return validate_grafx_page_size(value)

    @field_validator("kg_grafx_descriptor_revalidation", mode="before")
    @classmethod
    def _validate_grafx_descriptor_revalidation(cls, value: object) -> str:
        return validate_grafx_descriptor_revalidation(value)

    @property
    def kg_ladybug_buffer_pool_mb(self) -> int:
        """Provider-neutral spelling of the legacy board pool setting."""

        return self.kg_kuzu_buffer_pool_mb

    @property
    def kg_global_ladybug_buffer_pool_mb(self) -> int:
        """Provider-neutral spelling of the legacy Global pool setting."""

        return self.kg_global_kuzu_buffer_pool_mb

    @property
    def kg_ladybug_max_db_size_gb(self) -> int:
        """Provider-neutral spelling of the legacy storage-limit setting."""

        return self.kg_kuzu_max_db_size_gb

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
