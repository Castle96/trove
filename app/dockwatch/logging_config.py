"""Structured (key-value) logging configuration."""

from __future__ import annotations

import json
import logging
import logging.config
import time
from contextvars import ContextVar
from typing import Any, Literal

#: Per-request correlation ID, set by RequestIDMiddleware in app.main.
request_id_var: ContextVar[str | None] = ContextVar("dockwatch_request_id", default=None)


class KeyValueFormatter(logging.Formatter):
    """One JSON object per line with the core record fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "func": record.funcName,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO",
) -> None:
    """Configure the root logger with a key-value formatter at ``level``."""
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"kv": {"()": KeyValueFormatter}},
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "kv",
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {"handlers": ["console"], "level": level},
            "loggers": {
                # Silence uvicorn's access log; AccessLogMiddleware already logs
                # method/path/status/duration in the same key-value format.
                "uvicorn.access": {
                    "handlers": ["console"],
                    "level": "WARNING",
                    "propagate": False,
                },
            },
        }
    )
