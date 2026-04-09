"""Community edition settings — local-first, SQLite, ~/.okto-pulse/."""

from pathlib import Path
from pydantic import model_validator
from okto_pulse.core.infra.config import CoreSettings

class CommunitySettings(CoreSettings):
    """Settings for the community edition (local-first, single-user)."""

    data_dir: str = ""  # Default set in validator

    @model_validator(mode="after")
    def _derive_paths(self) -> "CommunitySettings":
        if not self.data_dir:
            self.data_dir = str(Path.home() / ".okto-pulse")
        data_path = Path(self.data_dir)
        # Only override if still at default values
        if self.database_url == "sqlite+aiosqlite:///./dashboard.db":
            db_path = data_path / "data" / "pulse.db"
            self.database_url = f"sqlite+aiosqlite:///{db_path}"
        if self.upload_dir == "./uploads":
            self.upload_dir = str(data_path / "uploads")
        # Default CORS for local dev
        if self.cors_origins == "http://localhost:5173,http://localhost:3000":
            self.cors_origins = f"http://localhost:{self.port},http://127.0.0.1:{self.port},http://localhost:5173,http://localhost:5174,http://localhost:3000"
        return self
