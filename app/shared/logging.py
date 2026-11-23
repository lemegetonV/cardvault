"""JSON logging with the correlation ID on every line, and redaction of card data.

PANs (13 to 19 digit runs), token references and bearer tokens are masked by the
formatter itself, so a careless ``log.info(f"... {pan}")`` cannot leak them.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

from shared import correlation

_PAN = re.compile(r"(?<!\d)\d{13,19}(?!\d)")
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")
_TOKEN_REF = re.compile(r"\btok_[A-Za-z0-9]+")

# Attributes every LogRecord has; anything else passed via ``extra=`` is emitted as a field.
_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


def redact(text: str) -> str:
    text = _JWT.sub("[REDACTED_JWT]", text)
    text = _PAN.sub("[REDACTED_PAN]", text)
    return _TOKEN_REF.sub("[REDACTED_TOKEN]", text)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": correlation.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        line = json.dumps(entry, default=str)
        # Seeded bug hook: BUG_PAN_IN_LOGS (phase 6) marks records that skip redaction.
        if getattr(record, "_skip_redaction", False):
            return line
        return redact(line)


def setup(service: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Library loggers are noisy (SQLAlchemy logs every statement at INFO); keep them quiet.
    for noisy in ("aiokafka", "kafka", "sqlalchemy", "httpx", "httpcore", "asyncio", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # We log requests ourselves (with correlation IDs); silence uvicorn's access log.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
