"""JSON-lines logging with a per-request trace id.

Every line carries `trace_id`, taken from a context variable that the request
middleware sets. The same id is handed to the MCP server process, so one agent
request can be followed across `api.jsonl` and `mcp_tools.jsonl`
(Week 6 scripts/trace_rebuild.py).
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "component": self.component,
            "trace_id": trace_id_var.get(),
            "event": record.getMessage(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(component: str, log_dir: Path, filename: str, stream=sys.stdout) -> logging.Logger:
    """Send JSON lines to `stream` and to `log_dir/filename`. Idempotent."""
    logger = logging.getLogger(component)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = JsonFormatter(component)

    stream_handler = logging.StreamHandler(stream)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / filename, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:  # read-only filesystem: keep stream logging only
        logger.warning("file_logging_disabled", extra={"reason": str(exc)})
    return logger
