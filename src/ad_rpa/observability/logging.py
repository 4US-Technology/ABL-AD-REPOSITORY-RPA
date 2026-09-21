from __future__ import annotations
import json
import logging
import sys
from datetime import datetime, timezone
from uuid import uuid4
_SENSITIVE = ("password", "secret", "token", "email", "login", "user", "dn")
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {"timestamp": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
                "flow": getattr(record, "flow", "service"), "event": getattr(record, "event", record.msg),
                "run_id": getattr(record, "run_id", "-")}
        if record.exc_info:
            data["error"] = type(record.exc_info[1]).__name__
        for key, value in getattr(record, "fields", {}).items():
            if not any(word in key.lower() for word in _SENSITIVE):
                data[key] = value
        return json.dumps(data, ensure_ascii=False)
def get_logger(flow: str, run_id: str | None = None) -> logging.LoggerAdapter:
    logger = logging.getLogger("ad_rpa")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logging.LoggerAdapter(logger, {"flow": flow, "run_id": run_id or uuid4().hex})
