"""Structured logging setup for WiSense-Twin.

Provides configure_logging() which supports both human-readable text output and
machine-parseable JSON formatted logs, applying the level and format specified
in AppConfig.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from wisense_backend.config.app_config import AppConfig


class JsonFormatter(logging.Formatter):
    """Formats log records as structured single-line JSON strings."""

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record as a JSON object."""
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # Include custom extra fields if provided
        standard_attrs = {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "taskName", "message",
        }
        for key, val in record.__dict__.items():
            if key not in standard_attrs and not key.startswith("_"):
                try:
                    # Test JSON serializability; fallback to string if not serializable
                    json.dumps(val)
                    payload[key] = val
                except (TypeError, OverflowError):
                    payload[key] = str(val)

        return json.dumps(payload)


class TextFormatter(logging.Formatter):
    """Standard human-readable text formatter."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


def configure_logging(config: AppConfig) -> logging.Logger:
    """Configure structured logging for the WiSense backend.

    Args:
        config: Application configuration defining log_level and log_format.

    Returns:
        The configured 'wisense_backend' Logger instance.
    """
    level_name = config.log_level.upper()
    level = getattr(logging, level_name, logging.INFO)

    logger = logging.getLogger("wisense_backend")
    logger.setLevel(level)

    # Clean existing handlers to ensure idempotent reconfiguration
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(level)

    if config.log_format == "json":
        stream_handler.setFormatter(JsonFormatter())
    else:
        stream_handler.setFormatter(TextFormatter())

    logger.addHandler(stream_handler)
    logger.propagate = False

    return logger
