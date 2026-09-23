"""Date-stamped file logging used by PersonZit services and integrations."""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from .config import home, load_config


class DatedFileHandler(logging.Handler):
    """Write to ``<prefix>-YYYYMMDD.log`` and switch files at local midnight."""

    def __init__(self, directory: Path, prefix: str, encoding: str = "utf-8"):
        super().__init__()
        self.directory = directory
        self.prefix = prefix
        self.encoding = encoding
        self._date = ""
        self._stream = None
        self.directory.mkdir(parents=True, exist_ok=True)

    def _stream_for_now(self) -> None:
        date = datetime.now().strftime("%Y%m%d")
        if date == self._date and self._stream is not None:
            return
        if self._stream is not None:
            self._stream.close()
        self._date = date
        path = self.directory / f"{self.prefix}-{date}.log"
        self._stream = path.open("a", encoding=self.encoding, errors="replace")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            with self.lock:
                self._stream_for_now()
                assert self._stream is not None
                self._stream.write(message + "\n")
                self._stream.flush()
        except Exception:  # Logging must never interrupt task processing.
            self.handleError(record)

    def close(self) -> None:
        with self.lock:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        super().close()


def configured_log_directory() -> Path:
    configured = os.getenv("PERSONZIT_LOG_DIR") or load_config().get("logging", {}).get("directory") or "logs"
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = home() / path
    return path


def log_file_path(category: str) -> Path:
    return configured_log_directory() / f"{category}-{datetime.now():%Y%m%d}.log"


def _clean_old_logs(directory: Path, prefix: str, retention_days: int) -> None:
    if retention_days <= 0 or not directory.exists():
        return
    cutoff = datetime.now().date() - timedelta(days=retention_days)
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d{{8}})\.log$")
    for item in directory.iterdir():
        match = pattern.match(item.name)
        if not match:
            continue
        try:
            if datetime.strptime(match.group(1), "%Y%m%d").date() < cutoff:
                item.unlink()
        except (OSError, ValueError):
            continue


def configure_logger(category: str, level: str | None = None) -> logging.Logger:
    """Get a logger whose file name and every record are timestamped."""
    cfg = load_config().get("logging", {})
    logger = logging.getLogger(f"personzit.{category}")
    if getattr(logger, "_personzit_file_configured", False):
        return logger

    directory = configured_log_directory()
    handler = DatedFileHandler(directory, category)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(handler)
    logger.setLevel((level or os.getenv("PERSONZIT_LOG_LEVEL") or cfg.get("level") or "INFO").upper())
    logger.propagate = False
    logger._personzit_file_configured = True  # type: ignore[attr-defined]
    _clean_old_logs(directory, category, int(cfg.get("retention_days", 30)))
    return logger