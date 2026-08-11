from __future__ import annotations

import logging

from riverhog_api.app import _configure_logging


def test_configured_log_level_covers_transfer_telemetry() -> None:
    transfer_logger = logging.getLogger("riverhog.transfer")
    original_level = transfer_logger.level
    try:
        _configure_logging("INFO")
        assert transfer_logger.level == logging.INFO
    finally:
        transfer_logger.setLevel(original_level)
