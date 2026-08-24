from __future__ import annotations

import re

from application_access.access import *  # noqa: F403
from application_access.access import __all__ as _access_exports

_APP_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def normalize_app_name(value: str) -> str:
    name = value.strip().casefold()
    if not name or _APP_PATTERN.fullmatch(name) is None:
        raise ValueError("app name must use lowercase letters, digits, and single dashes")
    return name


__all__ = ["normalize_app_name", *_access_exports]
