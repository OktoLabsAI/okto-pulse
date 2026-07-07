"""Community edition settings — local-first, SQLite, ~/.okto-pulse/."""

import os
from pathlib import Path
from pydantic import model_validator
from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.community.adapters.telemetry_effect_config import (
    COMMUNITY_DEFAULT_METRICS_BEACON_URL,
)

class CommunitySettings(CoreSettings):
    """Settings for the community edition (local-first, single-user)."""

    host: str = "127.0.0.1"  # Community is local-only — bind to loopback
    data_dir: str = ""  # Default set in validator
    metrics_beacon_url: str = COMMUNITY_DEFAULT_METRICS_BEACON_URL

    # Community ships sentence-transformers as a mandatory dep (pyproject.toml),
    # so override the core default of "stub" — semantic KG search needs real
    # embeddings out of the box. Users can still flip to "stub" via env.
    kg_embedding_mode: str = "sentence-transformers"

    @model_validator(mode="after")
    def _derive_paths(self) -> "CommunitySettings":
        if not self.data_dir:
            self.data_dir = os.environ.get("OKTO_PULSE_HOME") or str(
                Path.home() / ".okto-pulse"
            )
        data_path = Path(self.data_dir).expanduser().resolve()
        self.data_dir = str(data_path)
        # Only override if still unset or at the legacy core default value.
        if not self.database_url or self.database_url == "sqlite+aiosqlite:///./dashboard.db":
            db_path = data_path / "data" / "pulse.db"
            self.database_url = f"sqlite+aiosqlite:///{db_path}"
        if not self.upload_dir or self.upload_dir == "./uploads":
            self.upload_dir = str(data_path / "uploads")
        if not self.metrics_dir:
            self.metrics_dir = str(data_path / "metrics")
        default_kg_base = str(CoreSettings.model_fields["kg_base_dir"].default)
        if not self.kg_base_dir or self.kg_base_dir == default_kg_base:
            self.kg_base_dir = str(data_path)
        else:
            self.kg_base_dir = str(Path(self.kg_base_dir).expanduser().resolve())
        # Community edition is local-only — allow all origins to avoid CORS
        # issues regardless of which port the user configures via CLI
        self.cors_origins = "*"
        return self
