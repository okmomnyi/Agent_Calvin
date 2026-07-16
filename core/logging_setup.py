"""Central logging configuration for AgentOS.

Installs a rotating file handler (logs/agentos.log) plus a console handler, using
`rich` for colourised console output when available and falling back to plain
formatting otherwise. Call configure_logging() once at process startup.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from core.config import get_settings

_CONFIGURED = False


def configure_logging(level: int = logging.INFO) -> None:
    """Idempotently configure root logging with rotating file + console handlers."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    settings = get_settings()
    log_file = settings.log_dir / "agentos.log"

    root = logging.getLogger()
    root.setLevel(level)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root.addHandler(file_handler)

    try:  # rich console handler when available; plain StreamHandler otherwise
        from rich.logging import RichHandler

        console: logging.Handler = RichHandler(rich_tracebacks=True, show_path=False)
        console.setFormatter(logging.Formatter("%(message)s"))
    except ImportError:  # pragma: no cover - rich is optional
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(console)

    # tame noisy third-party loggers
    for noisy in ("httpx", "httpx2", "urllib3", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a named logger, ensuring logging is configured first."""
    configure_logging()
    return logging.getLogger(name)
