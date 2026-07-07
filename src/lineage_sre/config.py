"""Settings loaded from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Repo root when running from a source checkout (src/lineage_sre/config.py -> repo)
_REPO_ROOT = Path(__file__).resolve().parents[2]

PLATFORM = "duckdb"
ENV = "PROD"
DATASET_PREFIX = "demo"  # DataHub dataset names look like demo.<table>


def _models_dir() -> Path:
    candidate = _REPO_ROOT / "demo" / "models"
    if candidate.is_dir():
        return candidate
    return Path.cwd() / "demo" / "models"


@dataclass
class Settings:
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    model: str = field(default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5"))
    gms_url: str = field(default_factory=lambda: os.getenv("DATAHUB_GMS_URL", "http://localhost:8080"))
    datahub_token: str = field(default_factory=lambda: os.getenv("DATAHUB_TOKEN", ""))
    warehouse_path: Path = field(
        default_factory=lambda: Path(os.getenv("LINEAGE_SRE_WAREHOUSE", "demo_warehouse.duckdb"))
    )
    models_dir: Path = field(default_factory=_models_dir)
    fixes_dir: Path = field(default_factory=lambda: Path.cwd() / "fixes")
    reports_dir: Path = field(default_factory=lambda: Path.cwd() / "reports")


def get_settings() -> Settings:
    return Settings()
