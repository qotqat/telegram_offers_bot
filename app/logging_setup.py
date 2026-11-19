"""Logging setup: rotating file + console, UTF-8 safe on Windows consoles."""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable, Optional

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


class RedactingFormatter(logging.Formatter):
    """Redact after formatting so exceptions and third-party URLs are covered."""

    def __init__(self, secrets: Iterable[Optional[str]] = ()) -> None:
        super().__init__(_FORMAT)
        self.secrets = sorted({value for value in secrets if value}, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"(?<!\d)\d{5,}:[A-Za-z0-9_-]{20,}", "[BOT_TOKEN]", text)
        return re.sub(r"amzn1\.oa2-cs\.v1\.[A-Za-z0-9._-]+", "[AMAZON_SECRET]", text)


def process_log_path(log_path: str | Path, component: str) -> Path:
    """Never let independent rotating handlers share the same log file."""
    path = Path(log_path)
    if path.stem == component or path.stem.endswith("-" + component):
        return path
    return path.with_name(f"{path.stem}-{component}{path.suffix or '.log'}")


def setup_logging(
    log_path: str | Path,
    level: str = "INFO",
    *,
    max_bytes: int = 5 * 1024 * 1024,
    backups: int = 5,
    secrets: Iterable[Optional[str]] = (),
) -> logging.Logger:
    """Configure the root logger once and return it."""
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Windows consoles default to a legacy code page; deal titles include
    # Arabic text and emoji, so force UTF-8 (with replacement) when possible.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - unusual streams
                pass

    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = RedactingFormatter(secrets)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Third-party libraries are chatty at DEBUG/INFO.
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return root
