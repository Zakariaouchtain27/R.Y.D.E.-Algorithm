import json
import logging
import os
from datetime import datetime, timezone

_SKIP_KEYS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName",
})


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict = {
            "ts":      datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level":   record.levelname,
            "logger":  record.name,
            "msg":     record.getMessage(),
        }
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        for k, v in record.__dict__.items():
            if k not in _SKIP_KEYS:
                out[k] = v
        return json.dumps(out, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=[handler],
        force=True,
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
