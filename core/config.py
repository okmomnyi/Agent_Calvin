"""Central configuration loader for AgentOS.

Loads non-secret settings from config.yaml and secrets from the process environment
(.env is read via python-dotenv when present). Exposes a cached Settings object so
every module reads one consistent view of config without re-parsing files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

try:  # optional: load .env if python-dotenv is installed and a .env exists
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is a declared dep but keep import soft
    load_dotenv = None  # type: ignore[assignment]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass(frozen=True)
class Settings:
    """Resolved runtime settings — a merge of environment secrets and config.yaml."""

    # secrets / identity (environment)
    nvidia_api_key: str
    my_name: str
    my_email: str
    telegram_bot_token: str
    telegram_chat_id: str
    ws_token: str
    serpapi_key: str

    # server
    host: str
    port: int
    tz: str

    # paths
    project_root: Path
    data_dir: Path
    log_dir: Path

    # database (PostgreSQL — raw SQL via psycopg, no ORM)
    database_url: str

    # config.yaml (whole tree, plus convenience views)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def llm(self) -> dict[str, Any]:
        return self.raw.get("llm", {})

    @property
    def llm_routes(self) -> dict[str, Any]:
        return dict(self.llm.get("routes", {}))

    def get(self, *keys: str, default: Any = None) -> Any:
        """Dotted-path lookup into config.yaml, e.g. settings.get('jobs', 'score_threshold')."""
        node: Any = self.raw
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


# Placeholder values shipped in config.yaml's own comments/examples. If one of these is still
# live, it was never actually replaced -- and unlike most bad config, this class of mistake is
# silent: the app runs fine, the placeholder just quietly reaches the daily briefing forever
# (the exact case that motivated this: "Example Co" and "Local tech hub" turned up in every
# single morning briefing because config.yaml's shipped example was never edited).
KNOWN_SEED_VALUES = {
    "Example Co — client web app",
    "Local tech hub — volunteer",
}


def seed_data_warnings(settings: "Settings") -> list[str]:
    """Config values that still match shipped example text — never actually filled in.
    Checked at startup and surfaced in /api/health so this can't silently persist (§0: a
    fake commitment reported as real is exactly the kind of thing that must not happen)."""
    warnings: list[str] = []
    commitments = settings.get("planner", "commitments", default=[]) or []
    seeded = [c for c in commitments if c in KNOWN_SEED_VALUES]
    if seeded:
        warnings.append(
            f"config.yaml planner.commitments still has the shipped example value(s), "
            f"never replaced: {seeded}")
    return warnings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build (and cache) the Settings object. Call this everywhere instead of reading os.environ."""
    if load_dotenv is not None:
        env_file = PROJECT_ROOT / ".env"
        if env_file.exists():
            load_dotenv(env_file)

    raw = _load_yaml(CONFIG_PATH)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    return Settings(
        nvidia_api_key=os.getenv("NVIDIA_API_KEY", ""),
        my_name=os.getenv("MY_NAME", "Calvin"),
        my_email=os.getenv("MY_EMAIL", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        ws_token=os.getenv("AGENT_WS_TOKEN", ""),
        serpapi_key=os.getenv("SERPAPI_KEY", ""),
        host=os.getenv("AGENTOS_HOST", "0.0.0.0"),
        port=int(os.getenv("AGENTOS_PORT", "8000")),
        tz=os.getenv("AGENTOS_TZ", raw.get("timezone", "Africa/Nairobi")),
        project_root=PROJECT_ROOT,
        data_dir=DATA_DIR,
        log_dir=LOG_DIR,
        database_url=os.getenv(
            "DATABASE_URL", "postgresql://agentos:agentos@localhost:5432/agentos"),
        raw=raw,
    )
