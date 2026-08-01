from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

from riverhog_protocol.errors import BadRequest

PLAN_TTL = timedelta(minutes=15)


def plan_challenge(
    prefix: str,
    plan: dict[str, object],
    expires_at: datetime,
) -> str:
    payload = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{prefix}-{int(expires_at.timestamp())}-{hashlib.sha256(payload).hexdigest()}"


def challenge_expiry(challenge: str, *, prefix: str, operation: str) -> datetime:
    match = re.fullmatch(rf"{re.escape(prefix)}-(\d+)-([0-9a-f]{{64}})", challenge)
    if match is None:
        raise BadRequest(f"invalid {operation} challenge")
    return datetime.fromtimestamp(int(match.group(1)), tz=UTC)


def challenge_has_shape(challenge: str, *, prefix: str) -> bool:
    return re.fullmatch(rf"{re.escape(prefix)}-\d+-[0-9a-f]{{64}}", challenge) is not None


__all__ = ["PLAN_TTL", "challenge_expiry", "challenge_has_shape", "plan_challenge"]
