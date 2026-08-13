from __future__ import annotations

import hashlib
import json

from riverhog_core.state_migrations.versions.v1_0003 import _normalize_retrieval_plan


def test_retrieval_plan_migration_establishes_the_current_v1_policy() -> None:
    migrated = _normalize_retrieval_plan(
        {
            "format": "riverhog-retrieval-plan/v1",
            "lease_seconds": 3600,
            "files": [],
            "objects": [{"read_mode": "restore_required"}],
            "etag": "a" * 64,
        }
    )

    etag = str(migrated.pop("etag"))
    assert migrated["restore_policy"] == "allow"
    assert migrated["requires_restore"] is True
    assert (
        etag
        == hashlib.sha256(
            json.dumps(migrated, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
