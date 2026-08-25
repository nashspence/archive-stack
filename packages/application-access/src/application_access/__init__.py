from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, Field

from application_access.access import *  # noqa: F403
from application_access.access import __all__ as _access_exports

APPLICATION_NAME_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
APPLICATION_KEY_ID_PATTERN = r"^[0-9a-f]{16}$"
_APP_PATTERN = re.compile(APPLICATION_NAME_PATTERN)
_KEY_PATTERN = re.compile(APPLICATION_KEY_ID_PATTERN)


def validate_application_name(value: str) -> str:
    if not value or _APP_PATTERN.fullmatch(value) is None:
        raise ValueError("app name must use lowercase letters, digits, and single dashes")
    return value


def validate_application_key_id(value: str) -> str:
    if not value or _KEY_PATTERN.fullmatch(value) is None:
        raise ValueError("application key ID must be exactly 16 lowercase hexadecimal digits")
    return value


type ApplicationName = Annotated[
    str,
    Field(pattern=APPLICATION_NAME_PATTERN),
    AfterValidator(validate_application_name),
]
type ApplicationKeyId = Annotated[
    str,
    Field(pattern=APPLICATION_KEY_ID_PATTERN),
    AfterValidator(validate_application_key_id),
]


__all__ = [
    "APPLICATION_KEY_ID_PATTERN",
    "APPLICATION_NAME_PATTERN",
    "ApplicationKeyId",
    "ApplicationName",
    "validate_application_key_id",
    "validate_application_name",
    *_access_exports,
]
