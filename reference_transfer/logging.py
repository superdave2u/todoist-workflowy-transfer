from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any


def setup_logging() -> logging.Logger:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return logging.getLogger("reference_transfer")


def event(logger: logging.Logger, name: str, **fields: Any) -> None:
    logger.info(json.dumps({"timestamp": datetime.now(UTC).isoformat(), "event": name, **fields}, default=str))
