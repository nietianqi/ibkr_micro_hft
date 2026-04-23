from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .config import EngineConfig


def _normalize(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: _normalize(raw) for key, raw in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _normalize(raw) for key, raw in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_normalize(raw) for raw in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in logging.LogRecord("", 0, "", 0, "", (), None).__dict__
        }
        if extras:
            payload["extra"] = _normalize(extras)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(config: EngineConfig) -> None:
    logging.getLogger().handlers.clear()
    level = getattr(logging, config.logging.level.upper(), logging.INFO)
    handler = logging.StreamHandler()
    if config.logging.json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=level, handlers=[handler])
    Path(config.storage.root_dir).mkdir(parents=True, exist_ok=True)
