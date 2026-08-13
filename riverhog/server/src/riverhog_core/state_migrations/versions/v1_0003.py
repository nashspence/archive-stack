"""Normalize persisted retrieval plans to the current v1 policy contract."""

import hashlib
import json
from typing import Any

from alembic import op
from sqlalchemy import text

revision: str = "v1_0003"
down_revision: str | None = "v1_0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    connection = op.get_bind()
    rows = list(
        connection.execute(text("SELECT id, constraints_json FROM retrieval_jobs")).mappings()
    )
    for row in rows:
        plan = _normalize_retrieval_plan(json.loads(str(row["constraints_json"])))
        connection.execute(
            text(
                "UPDATE retrieval_jobs "
                "SET plan_etag = :plan_etag, constraints_json = :constraints_json "
                "WHERE id = :job_id"
            ),
            {
                "job_id": str(row["id"]),
                "plan_etag": str(plan["etag"]),
                "constraints_json": json.dumps(plan, sort_keys=True, separators=(",", ":")),
            },
        )


def _normalize_retrieval_plan(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("persisted retrieval plan must be an object")
    plan = dict(value)
    objects = plan.get("objects")
    if not isinstance(objects, list) or any(not isinstance(current, dict) for current in objects):
        raise ValueError("persisted retrieval plan objects are invalid")
    plan.pop("etag", None)
    plan["restore_policy"] = "allow"
    plan["requires_restore"] = any(
        current.get("read_mode") == "restore_required" for current in objects
    )
    plan["etag"] = hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return plan


def downgrade() -> None:
    raise RuntimeError("Riverhog state migrations are forward-only")
