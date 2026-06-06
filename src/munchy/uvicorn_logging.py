from __future__ import annotations

import copy
import logging
from typing import Any

import uvicorn.config


class DropHealthAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        path = ""
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            path = str(record.args[2])
        if not path:
            path = record.getMessage()
        return "/health/live" not in path and "/health/ready" not in path


def uvicorn_log_config_without_health_access_logs() -> dict[str, Any]:
    config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    filters = config.setdefault("filters", {})
    filters["drop_health_access"] = {"()": DropHealthAccessLogFilter}
    handlers = config.setdefault("handlers", {})
    access_handler = handlers.get("access")
    if isinstance(access_handler, dict):
        configured_filters = list(access_handler.get("filters", []))
        if "drop_health_access" not in configured_filters:
            configured_filters.append("drop_health_access")
        access_handler["filters"] = configured_filters
    return config
