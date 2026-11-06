"""Validated configuration without import-time filesystem or database changes."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    public_origin: str = "http://localhost:5173"
    data_dir: Path = ROOT / ".local/data"
    backup_dir: Path = ROOT / ".local/backups"
    provider_secret_dir: Path = ROOT / "secrets"
    minimum_free_bytes: int = 100 * 1024 * 1024

    @model_validator(mode="after")
    def validate_origin(self) -> "Settings":
        origin = urlsplit(self.public_origin)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.username is not None
            or origin.password is not None
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
        ):
            raise ValueError("PUBLIC_ORIGIN must be an HTTP(S) origin without credentials or paths")
        if origin.scheme != "https" and (
            self.app_env == "production"
            or origin.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}
        ):
            raise ValueError("HTTPS is required except for loopback development")
        self.public_origin = self.public_origin.rstrip("/")
        if self.minimum_free_bytes < 0:
            raise ValueError("minimum_free_bytes cannot be negative")
        return self

    @property
    def database_path(self) -> Path:
        return self.data_dir / "portfolio.sqlite3"
