"""Configuration loading for development and container deployments."""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'") )
def _load_secret_files(directory: Path) -> None:
    """Load Docker secrets using NAME_FILE=/run/secrets/name or VAR_FILE_DIR."""
    if directory.exists():
        for secret in directory.iterdir():
            if secret.is_file():
                os.environ.setdefault(secret.name.upper().replace("-", "_"), secret.read_text().strip())
    for key, value in list(os.environ.items()):
        if key.endswith("_FILE") and value:
            path = Path(value)
            if path.is_file():
                os.environ[key[:-5]] = path.read_text(encoding="utf-8").strip()
def _load_var_file() -> None:
    value = os.getenv("VAR_FILE")
    if not value: return
    path = Path(value)
    if path.is_file(): _load_dotenv(path)
    elif path.is_dir(): _load_secret_files(path)
@dataclass(frozen=True)
class Settings:
    db_path: str = "/data/ad-rpa.db"
    poll_interval: int = 60
    timezone: str = "America/Sao_Paulo"
    metrics_host: str = "127.0.0.1"
    metrics_port: int = 8080
    @classmethod
    def load(cls, *, load_dotenv: bool = True) -> "Settings":
        if load_dotenv:
            _load_dotenv(Path(os.getenv("ENV_FILE", ".env")))
        _load_var_file()
        _load_secret_files(Path(os.getenv("VAR_FILE_DIR", "/run/secrets")))
        return cls(
            db_path=os.getenv("AD_RPA_DB_PATH", "/data/ad-rpa.db"),
            poll_interval=int(os.getenv("AD_RPA_POLL_INTERVAL", "60")),
            timezone=os.getenv("AD_RPA_TIMEZONE", "America/Sao_Paulo"),
            metrics_host=os.getenv("AD_RPA_HEALTH_HOST", "127.0.0.1"),
            metrics_port=int(os.getenv("AD_RPA_HEALTH_PORT", "8080")),
        )
