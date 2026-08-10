from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import jeb_core.adapters.munchy as munchy_adapter_module
import jeb_core.domain.models as domain_models
import pytest
from jeb_api.composition import JebServices, config_from_env, create_services
from jeb_core.adapters.munchy import MunchyTargetAdapter
from jeb_core.domain.models import (
    EligibleFile,
    parse_duration,
    parse_size,
)
from jeb_core.domain.sources import SourceRegistryError
from jeb_core.persistence.schema import upgrade_state
from lifecycle_events import cloud_event

TEST_TEMPLATE = "camera-archive"


def env_for(tmp_path: Path, *, sources: str = "camera,phone") -> dict[str, str]:
    return {
        "TEST_SOURCE_IDS": sources,
        "JEB_LANDING_DIR": str(tmp_path / "landing"),
        "JEB_STATE_DIR": str(tmp_path / "state"),
        "JEB_MUNCHY_URL": "http://munchy.test",
        "JEB_FTP_UID": str(os.getuid()),
        "JEB_FTP_GID": str(os.getgid()),
    }


def services_from_env(
    env: dict[str, str],
    *,
    target_adapters=None,
    options: dict[str, dict[str, object]] | None = None,
    templates: dict[str, str] | None = None,
) -> JebServices:
    source_ids = env.get("TEST_SOURCE_IDS", "").split(",")
    runtime_env = {key: value for key, value in env.items() if not key.startswith("TEST_")}
    services = create_services(
        config_from_env(runtime_env),
        target_adapters=target_adapters,
    )
    upgrade_state(services.config)
    services.runtime.initialize()
    for source_id in source_ids:
        if not source_id:
            continue
        source_options = dict((options or {}).get(source_id, {}))
        services.sources.add_source(
            source_id,
            adapters=("ftp", "tus"),
            target_config={"template_id": (templates or {}).get(source_id, TEST_TEMPLATE)},
            credential=f"{source_id}-password",
            stable_seconds=0,
            include_extensions=(".txt", ".mp4", ".xml"),
            **source_options,
        )
    return services


def write_stable_file(path: Path, content: bytes = b"data") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    old = time.time() - 14 * 86_400
    os.utime(path, (old, old))


def set_current_time(
    monkeypatch: pytest.MonkeyPatch,
    services: JebServices,
    value: datetime,
) -> None:
    monkeypatch.setattr(services.sources, "current_time", lambda: value)
    monkeypatch.setattr(services.attempts, "current_time", lambda: value)


def unresolved_attempt_ids(services: JebServices) -> list[str]:
    return [str(row["id"]) for row in services.store.unresolved_attempts()]


@dataclass
class CompleteAdapter(MunchyTargetAdapter):
    calls: int = 0

    def advance(self, services: JebServices, attempt_id: str) -> None:
        self.calls += 1
        services.store.set_attempt_state(attempt_id, "target_complete")


@pytest.fixture(autouse=True)
def accept_target_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeMunchyClient:
        def __init__(
            self,
            url: str,
            *,
            token: str = "",
            allow_insecure_http: bool = False,
        ) -> None:
            self.url = url
            self.token = token
            self.allow_insecure_http = allow_insecure_http

        def preflight_submission(self, request: object) -> dict[str, object]:
            _ = request
            return {"accepted": True}

        def close(self) -> None:
            pass

    monkeypatch.setattr(munchy_adapter_module, "MunchyClient", FakeMunchyClient)


def test_parse_helpers_support_human_units() -> None:
    assert parse_duration("10m") == 600
    assert parse_duration("2h") == 7200
    assert parse_size("2GiB") == 2 * 1024**3


def test_runtime_config_and_source_registry_have_distinct_authority(tmp_path: Path) -> None:
    services = services_from_env(env_for(tmp_path))

    assert services.config.service.batch_dir == tmp_path / "landing" / ".jeb-batches"
    sources = services.source_registry.list()
    assert [source.id for source in sources] == ["camera", "phone"]
    assert [source.target_config for source in sources] == [
        {"template_id": TEST_TEMPLATE},
        {"template_id": TEST_TEMPLATE},
    ]

    with pytest.raises(SourceRegistryError, match="source already exists: camera"):
        services.sources.add_source(
            "camera",
            adapters=("tus",),
            target_config={"template_id": TEST_TEMPLATE},
            credential="replacement-password",
        )


def test_source_target_config_and_ftp_projection_are_registry_owned(tmp_path: Path) -> None:
    services = services_from_env(env_for(tmp_path, sources="camera"))
    source = services.source_registry.get("camera")
    ftp_home = tmp_path / "landing" / "camera"

    assert source.target_config == {"template_id": TEST_TEMPLATE}
    assert ftp_home.is_dir()
    assert ftp_home.stat().st_uid == os.getuid()
    assert ftp_home.stat().st_gid == os.getgid()
    assert ftp_home.stat().st_mode & 0o777 == 0o770
    projection = services.config.ingress.ftp_projection.read_text(encoding="utf-8")
    assert projection.startswith("camera:$argon2id$")

    ftp_home.chmod(0o755)
    services.source_registry.initialize()
    assert ftp_home.stat().st_mode & 0o777 == 0o770

    updated = services.sources.update_source(
        "camera",
        {
            "cadence": "manual",
            "target_config": {"template_id": "camera-review"},
        },
    )

    assert updated.cadence == "manual"
    assert updated.target_config == {"template_id": "camera-review"}


def test_source_registry_lists_compact_filtered_pages_in_sql(tmp_path: Path) -> None:
    services = services_from_env(env_for(tmp_path))
    services.source_registry.set_enabled("phone", False)
    services.source_registry.update(
        "phone",
        {
            "adapters": ["tus"],
            "target": "review",
            "cadence": "manual",
        },
    )

    page = services.source_registry.list_page(
        page=1,
        per_page=1,
        sort="id",
        order="desc",
    )

    assert page["total"] == 2
    assert page["pages"] == 2
    assert page["per_page"] == 1
    assert [source["id"] for source in page["sources"]] == ["phone"]
    assert page["sources"][0]["target_config"] == {"template_id": TEST_TEMPLATE}

    filtered = services.source_registry.list_page(
        query="REVIEW",
        enabled=False,
        adapter="tus",
        target="review",
        all_items=True,
    )

    assert filtered["page"] == 1
    assert filtered["per_page"] == 1
    assert filtered["pages"] == 1
    assert filtered["filters"] == {
        "enabled": False,
        "adapter": "tus",
        "target": "review",
    }
    assert [source["id"] for source in filtered["sources"]] == ["phone"]

    with pytest.raises(SourceRegistryError, match="between 1 and 100"):
        services.source_registry.list_page(per_page=101)


def test_jeb_schema_indexes_operator_status_and_list_paths(tmp_path: Path) -> None:
    services = services_from_env(env_for(tmp_path))

    with services.store.transaction() as conn:
        indexes = {
            table: {str(row["name"]) for row in conn.execute(f"PRAGMA index_list({table})")}
            for table in (
                "batches",
                "batch_attempts",
                "files",
                "attempt_files",
                "service_operations",
            )
        }
        batch_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(batches)")}
        triggers = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }

    assert {"file_count", "total_bytes"} <= batch_columns
    assert {
        "idx_jeb_batches_source",
        "idx_jeb_batches_source_period",
        "idx_jeb_batches_file_count",
        "idx_jeb_batches_target",
        "idx_jeb_batches_total_bytes",
    } <= indexes["batches"]
    assert {
        "idx_jeb_batch_attempts_batch_state",
        "idx_jeb_batch_attempts_created",
        "idx_jeb_batch_attempts_target_submission",
        "idx_jeb_batch_attempts_state",
        "idx_jeb_batch_attempts_state_updated",
        "idx_jeb_batch_attempts_updated",
    } <= indexes["batch_attempts"]
    assert "idx_jeb_files_batch" in indexes["files"]
    assert "idx_jeb_attempt_files_attempt" in indexes["attempt_files"]
    assert {
        "idx_jeb_service_operations_started",
        "idx_jeb_service_operations_state_started",
        "ux_jeb_service_operations_running",
    } <= indexes["service_operations"]
    assert {
        "trg_jeb_files_summary_delete",
        "trg_jeb_files_summary_insert",
        "trg_jeb_files_summary_update_moved_batch",
        "trg_jeb_files_summary_update_same_batch",
    } <= triggers


def test_jeb_batch_file_summaries_are_trigger_maintained_transactionally(
    tmp_path: Path,
) -> None:
    env = env_for(tmp_path, sources="camera")
    write_stable_file(tmp_path / "landing" / "camera" / "a.txt", b"aaa")
    write_stable_file(tmp_path / "landing" / "camera" / "b.txt", b"bbbbb")
    services = services_from_env(env)
    batch_id = services.attempts.archive_now(source_id="camera", process=False)
    assert batch_id is not None

    page = services.store.list_attempts(resolution="all", sort="bytes")
    assert page["attempts"][0]["file_count"] == 2
    assert page["attempts"][0]["total_bytes"] == 8

    with pytest.raises(RuntimeError):
        with services.store.transaction() as conn:
            conn.execute(
                "UPDATE files SET bytes = 99 WHERE batch_id = ? AND target_path = ?",
                (batch_id, "camera/a.txt"),
            )
            raise RuntimeError("rollback")
    page = services.store.list_attempts(resolution="all", sort="bytes")
    assert page["attempts"][0]["file_count"] == 2
    assert page["attempts"][0]["total_bytes"] == 8

    with services.store.transaction() as conn:
        conn.execute(
            "UPDATE files SET bytes = 7 WHERE batch_id = ? AND target_path = ?",
            (batch_id, "camera/a.txt"),
        )

    page = services.store.list_attempts(resolution="all", sort="bytes")
    assert page["attempts"][0]["file_count"] == 2
    assert page["attempts"][0]["total_bytes"] == 12

    with services.store.transaction() as conn:
        conn.execute(
            "DELETE FROM files WHERE batch_id = ? AND target_path = ?",
            (batch_id, "camera/b.txt"),
        )

    page = services.store.list_attempts(resolution="all", sort="bytes")
    assert page["attempts"][0]["file_count"] == 1
    assert page["attempts"][0]["total_bytes"] == 7

    assert services.store.list_attempts(resolution="all", source="camera")["total"] == 1

    with services.store.transaction() as conn:
        conn.execute(
            "DELETE FROM files WHERE batch_id = ? AND target_path = ?",
            (batch_id, "camera/a.txt"),
        )

    page = services.store.list_attempts(resolution="all", sort="bytes")
    assert page["attempts"][0]["source_id"] == "camera"
    assert page["attempts"][0]["file_count"] == 0
    assert page["attempts"][0]["total_bytes"] == 0
    assert services.store.list_attempts(resolution="all", source="camera")["total"] == 1


def test_operator_can_cancel_and_explicitly_retry_an_unresolved_attempt(tmp_path: Path) -> None:
    env = env_for(tmp_path, sources="camera")
    write_stable_file(tmp_path / "landing" / "camera" / "a.txt")
    services = services_from_env(env)
    attempt_id = services.attempts.archive_now(source_id="camera", process=False)
    assert attempt_id is not None

    canceled = services.attempts.cancel_attempt(attempt_id)

    assert canceled["state"] == "canceled"
    assert attempt_id not in unresolved_attempt_ids(services)
    event = services.event_log.page(after=None, limit=100).events[-1]
    assert event.type == "io.riverhog.jeb.attempt.canceled"
    assert event.subject == attempt_id
    retried_id = services.attempts.archive_now(source_id="camera", process=False)
    assert retried_id == f"{attempt_id}-r2"
    assert services.store.get_attempt(attempt_id)["state"] == "superseded"
    assert services.store.get_attempt(retried_id)["state"] == "batching"


def test_cancel_wins_a_race_with_target_completion(tmp_path: Path) -> None:
    env = env_for(tmp_path, sources="camera")
    write_stable_file(tmp_path / "landing" / "camera" / "a.txt")
    started = threading.Event()
    release = threading.Event()

    class BlockingAdapter(MunchyTargetAdapter):
        def advance(self, services: JebServices, attempt_id: str) -> None:
            started.set()
            assert release.wait(timeout=5)
            services.store.set_attempt_state(attempt_id, "target_complete")

        def cancel(self, services: JebServices, attempt_id: str) -> None:
            _ = services, attempt_id

    services = services_from_env(
        env,
        target_adapters={"munchy": BlockingAdapter()},
    )
    attempt_id = services.attempts.archive_now(source_id="camera", process=False)
    assert attempt_id is not None
    worker = threading.Thread(target=services.attempts.process_attempt, args=(attempt_id,))
    worker.start()
    assert started.wait(timeout=5)

    canceled = services.attempts.cancel_attempt(attempt_id)
    release.set()
    worker.join(timeout=5)

    assert canceled["state"] == "canceled"
    assert not worker.is_alive()
    assert services.store.get_attempt(attempt_id)["state"] == "canceled"


def test_env_config_loads_lifecycle_event_settings(tmp_path: Path) -> None:
    config = config_from_env(
        {
            **env_for(tmp_path, sources="camera"),
            "JEB_EVENT_SOURCE": "urn:test:jeb",
            "JEB_UPSTREAM_EVENT_POLL_SECONDS": "7",
            "JEB_EVENT_CONTEXT_RETENTION": "2d",
        }
    )

    assert config.events.source == "urn:test:jeb"
    assert config.events.upstream_poll_seconds == 7
    assert config.events.context_retention_seconds == 2 * 86_400


def test_munchy_target_uses_its_explicit_cleartext_transport_opt_in(tmp_path: Path) -> None:
    config = config_from_env(
        {
            **env_for(tmp_path, sources=""),
            "JEB_MUNCHY_ALLOW_INSECURE_HTTP": "true",
        }
    )

    target = config.targets["munchy"]
    client = munchy_adapter_module._munchy_client(target)
    try:
        assert target.allow_insecure_http is True
        assert client.url == "http://munchy.test"
        assert client.allow_insecure_http is True
    finally:
        client.close()


def test_source_registry_requires_safe_source_ids(tmp_path: Path) -> None:
    services = services_from_env(env_for(tmp_path, sources=""))

    with pytest.raises(SourceRegistryError, match="safe id"):
        services.sources.add_source(
            "phone/raw",
            adapters=("tus",),
            target_config={"template_id": TEST_TEMPLATE},
        )


def test_cleanup_after_success_requires_safe_munchy_target(tmp_path: Path) -> None:
    env = {**env_for(tmp_path, sources=""), "JEB_MUNCHY_WAIT_FOR_SAFE_DELETE": "false"}
    services = services_from_env(env)

    with pytest.raises(SourceRegistryError, match="safe-delete"):
        services.sources.add_source(
            "camera",
            adapters=("ftp",),
            target_config={"template_id": TEST_TEMPLATE},
            cleanup="after_target_success",
        )


def test_source_credential_rotation_updates_all_enabled_adapters(tmp_path: Path) -> None:
    services = services_from_env(env_for(tmp_path, sources="camera"))

    services.source_registry.authenticate("camera", "camera-password", adapter="ftp")
    services.source_registry.authenticate("camera", "camera-password", adapter="tus")
    _source, credential = services.source_registry.rotate_credential(
        "camera",
        credential="replacement-password",
    )

    assert credential is None
    with pytest.raises(SourceRegistryError, match="invalid Jeb ingress credentials"):
        services.source_registry.authenticate("camera", "camera-password", adapter="tus")
    services.source_registry.authenticate("camera", "replacement-password", adapter="ftp")
    assert "camera:$argon2id$" in services.config.ingress.ftp_projection.read_text(encoding="utf-8")


def test_source_purge_is_plan_bound_guarded_and_idempotent(tmp_path: Path) -> None:
    services = services_from_env(env_for(tmp_path, sources="camera"))
    landing_file = tmp_path / "landing" / "camera" / "clip.txt"
    write_stable_file(landing_file, b"first")
    batch_id = services.attempts.archive_now(source_id="camera", process=False)
    assert batch_id is not None

    blocked = services.sources.source_removal_plan("camera", purge=False)
    assert blocked["status"] == "blocked"
    assert blocked["challenge"] is None

    stale = services.sources.source_removal_plan("camera", purge=True)
    assert stale["status"] == "ready"
    assert stale["warning"] == domain_models.SOURCE_PURGE_WARNING
    landing_file.write_bytes(b"changed after planning")
    with pytest.raises(domain_models.UnrecoverableJebError, match="plan changed"):
        services.sources.remove_source("camera", challenge=stale["challenge"])

    plan = services.sources.source_removal_plan("camera", purge=True)
    result = services.sources.remove_source("camera", challenge=plan["challenge"])

    assert result == {
        "status": "removed",
        "source": "camera",
        "purged": True,
        "files": 1,
        "bytes": len(b"changed after planning"),
    }
    assert not landing_file.exists()
    with pytest.raises(SourceRegistryError, match="source not found"):
        services.source_registry.get("camera")
    assert services.sources.remove_source("camera", challenge=plan["challenge"]) == result


def test_scheduler_batches_each_source_independently(tmp_path: Path) -> None:
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt")
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt")
    adapter = CompleteAdapter()
    services = services_from_env(
        env_for(tmp_path),
        target_adapters={"munchy": adapter},
    )

    services.runtime.run_once()
    assert len(unresolved_attempt_ids(services)) == 2
    services.runtime.run_once()

    assert adapter.calls == 2
    with services.store.transaction() as conn:
        source_ids = [
            str(row["source_id"])
            for row in conn.execute("SELECT source_id FROM batches ORDER BY source_id")
        ]
    assert source_ids == ["camera", "phone"]


def test_manual_cadence_only_runs_with_archive_now(tmp_path: Path) -> None:
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt")
    services = services_from_env(
        env_for(tmp_path, sources="camera"),
        options={"camera": {"cadence": "manual"}},
    )

    services.runtime.run_once()
    assert unresolved_attempt_ids(services) == []

    assert services.attempts.archive_now(source_id="camera", process=False) is not None
    assert len(unresolved_attempt_ids(services)) == 1


def test_archive_plan_reports_batch_without_creating_it(tmp_path: Path) -> None:
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt", b"camera")
    services = services_from_env(env_for(tmp_path, sources="camera"))

    plan = services.attempts.archive_plan(source_id="camera")

    assert plan["status"] == "would_process"
    assert plan["dry_run"] is True
    assert plan["source"] == "camera"
    assert plan["target_name"] == "munchy"
    assert plan["file_count"] == 1
    assert plan["total_bytes"] == 6
    assert plan["batch_id"]
    assert plan["target_submission_id"]
    assert unresolved_attempt_ids(services) == []


def test_archive_plan_target_preflight_does_not_record_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_stable_file(tmp_path / "landing" / "camera" / "clip.mp4")
    services = services_from_env(env_for(tmp_path, sources="camera"))

    class RejectingMunchyClient:
        def __init__(
            self,
            url: str,
            *,
            token: str = "",
            allow_insecure_http: bool = False,
        ) -> None:
            self.url = url
            self.token = token
            self.allow_insecure_http = allow_insecure_http

        def preflight_submission(self, request: object) -> dict[str, object]:
            _ = request
            raise domain_models.UnrecoverableJebError("unknown template")

        def close(self) -> None:
            pass

    monkeypatch.setattr(munchy_adapter_module, "MunchyClient", RejectingMunchyClient)

    plan = services.attempts.archive_plan(source_id="camera")

    assert plan["status"] == "target_preflight_failed"
    assert plan["target_preflight"]["status"] == "rejected"
    assert "unknown template" in plan["target_preflight"]["error"]
    assert unresolved_attempt_ids(services) == []
    assert services.store.target_preflight_failures(state="failed") == []


def test_monthly_cadence_uses_first_scheduled_run_after_month_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = services_from_env(
        env_for(tmp_path, sources="phone"),
        options={"phone": {"cadence": "monthly", "weekday": 0, "hour": 3, "minute": 0}},
    )
    collection = services.source_registry.get("phone")

    set_current_time(monkeypatch, services, datetime(2026, 7, 8, 12, tzinfo=UTC))
    assert services.sources.source_period(collection) == datetime(2026, 7, 6, 3, tzinfo=UTC)

    set_current_time(monkeypatch, services, datetime(2026, 7, 2, 12, tzinfo=UTC))
    assert services.sources.source_period(collection) == datetime(2026, 6, 1, 3, tzinfo=UTC)


def test_seasonal_cadence_uses_first_scheduled_run_after_custom_season_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = services_from_env(
        env_for(tmp_path, sources="phone"),
        options={"phone": {"cadence": "seasonal", "weekday": 0, "hour": 3, "minute": 0}},
    )
    collection = services.source_registry.get("phone")

    set_current_time(monkeypatch, services, datetime(2026, 7, 8, 12, tzinfo=UTC))
    assert services.sources.source_period(collection) == datetime(2026, 6, 1, 3, tzinfo=UTC)

    set_current_time(monkeypatch, services, datetime(2026, 6, 1, 2, tzinfo=UTC))
    assert services.sources.source_period(collection) == datetime(2026, 3, 2, 3, tzinfo=UTC)


def test_munchy_submission_uses_template_and_generic_target_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = services_from_env(
        env_for(tmp_path, sources="camera"),
        templates={"camera": "camera-review"},
    )
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt")
    set_current_time(
        monkeypatch,
        services,
        datetime(2026, 7, 19, 16, 1, 2, 345678, tzinfo=UTC),
    )

    batch_id = services.attempts.archive_now(source_id="camera", process=False)
    assert batch_id is not None
    assert batch_id.startswith("20260719T160102Z__camera__")
    services.attempts.stage_attempt_files(batch_id)
    services.attempts.ensure_hashes(batch_id)
    services.attempts.ensure_provenance(batch_id)
    request = MunchyTargetAdapter().submission_request(
        services,
        batch_id,
        services.config.targets["munchy"],
    )

    assert request.submission_id == services.store.load_attempt(batch_id)["target_submission_id"]
    assert request.template_id == "camera-review"
    assert request.run_id == "20260719T160102Z"
    assert request.event_context == {"initiator": {"app": "jeb", "attempt_id": batch_id}}
    assert [item.rel_path for item in request.files] == ["camera/clip.txt"]
    assert request.files[0].sha256
    assert request.files[0].provenance["status"] == "captured"
    assert request.files[0].provenance_journals


def test_jeb_attempt_issue_is_appended_to_lifecycle_log(tmp_path: Path) -> None:
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt")
    services = services_from_env(env_for(tmp_path, sources="camera"))
    batch_id = services.attempts.archive_now(source_id="camera", process=False)
    assert batch_id is not None
    attempt = services.store.load_attempt(batch_id)

    assert services.events.emit_issue(
        context=dict(attempt),
        error="target failed",
        component="target",
        severity="critical",
    )

    page = services.event_log.page(after=None, limit=100)
    assert len(page.events) == 1
    assert page.events[0].type == "io.riverhog.jeb.attempt.issue"
    assert page.events[0].subject == batch_id
    assert page.events[0].data["source_id"] == "camera"
    assert page.events[0].data["error"] == "target failed"


def test_jeb_failed_attempt_reports_retained_source_as_error(tmp_path: Path) -> None:
    source_path = tmp_path / "landing" / "camera" / "clip.txt"
    write_stable_file(source_path)
    services = services_from_env(env_for(tmp_path, sources="camera"))
    attempt_id = services.attempts.archive_now(source_id="camera", process=False)
    assert attempt_id is not None

    services.attempts.mark_unrecoverable(attempt_id, "target failed", component="target")

    event = services.event_log.page(after=None, limit=100).events[0]
    assert event.data["severity"] == "error"
    assert source_path.is_file()


def test_jeb_target_preflight_events_and_status_use_source_context(tmp_path: Path) -> None:
    services = services_from_env(env_for(tmp_path, sources="camera,phone"))
    clean_statuses = {
        item["id"]: item
        for item in services.runtime.status_summary(include_backlog=False)["sources"]
    }
    assert clean_statuses["camera"]["target_preflight_failed"] is False
    assert clean_statuses["phone"]["target_preflight_failed"] is False

    source = services.source_registry.get("camera")
    file = EligibleFile(
        path=source.path / "clip.txt",
        rel=Path("clip.txt"),
        target_path="camera/clip.txt",
        bytes=4,
        mtime=1.0,
        mtime_ns=1,
    )
    services.store.store_target_preflight_failure(
        source=source,
        files=[file],
        failure_payload={"ok": False},
        fingerprint_payload={"source_id": "camera", "error": "unknown template"},
        message="unknown template",
    )
    services.sources.emit_target_preflight_failures(source_id="camera")

    event = services.event_log.page(after=None, limit=100).events[0]
    assert event.type == "io.riverhog.jeb.source.preflight_failed"
    assert event.subject == "camera"
    assert event.data["component"] == "target_preflight"
    failed_statuses = {
        item["id"]: item
        for item in services.runtime.status_summary(include_backlog=False)["sources"]
    }
    assert failed_statuses["camera"]["target_preflight_failed"] is True
    assert failed_statuses["phone"]["target_preflight_failed"] is False


def test_jeb_target_preflight_event_scan_is_bounded_and_fair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = services_from_env(env_for(tmp_path, sources=""))
    failures = [{"source_id": f"source-{index:03d}"} for index in range(101)]
    emitted: list[str] = []

    def page_failures(
        *,
        source_id: str | None = None,
        after_source_id: str | None = None,
        state: str = "failed",
        limit: int = 100,
    ) -> list[dict[str, str]]:
        assert state == "failed"
        rows = failures
        if source_id is not None:
            rows = [row for row in rows if row["source_id"] == source_id]
        if after_source_id is not None:
            rows = [row for row in rows if row["source_id"] > after_source_id]
        return rows[:limit]

    monkeypatch.setattr(services.store, "target_preflight_failures", page_failures)
    monkeypatch.setattr(
        services.sources,
        "resolve_inactive_target_preflight_failures",
        lambda: 0,
    )
    monkeypatch.setattr(
        services.events,
        "emit_target_preflight_failure",
        lambda row: emitted.append(str(row["source_id"])) or True,
    )

    services.sources.emit_target_preflight_failures()
    assert emitted == [f"source-{index:03d}" for index in range(100)]

    services.sources.emit_target_preflight_failures()
    assert emitted[-1] == "source-100"


def test_jeb_translates_owned_munchy_events_idempotently(tmp_path: Path) -> None:
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt")
    services = services_from_env(env_for(tmp_path, sources="camera"))
    attempt_id = services.attempts.archive_now(source_id="camera", process=False)
    assert attempt_id is not None
    attempt = services.store.load_attempt(attempt_id)
    job_id = str(attempt["target_submission_id"])
    upstream = cloud_event(
        source="urn:munchy",
        type="io.riverhog.munchy.job.archive.finalized",
        subject=job_id,
        data={
            "job_id": job_id,
            "collection_id": 41,
            "collection_created_at": "2026-06-05T12:00:00.000000Z",
            "collection_tags": ["camera", "family"],
            "context": {"notification_recipient": "katie"},
        },
    )

    adapter = MunchyTargetAdapter()
    assert adapter.translate_event(services, upstream)
    assert adapter.translate_event(services, upstream)

    page = services.event_log.page(after=None, limit=100)
    assert len(page.events) == 1
    event = page.events[0]
    assert event.type == "io.riverhog.jeb.attempt.target.archive.finalized"
    assert event.subject == attempt_id
    assert event.data["target_submission_id"] == job_id
    assert event.data["collection_id"] == 41
    assert event.data["collection_created_at"] == "2026-06-05T12:00:00.000000Z"
    assert event.data["collection_tags"] == ["camera", "family"]
    assert event.data["context"] == {"notification_recipient": "katie"}
    assert event.data["cause"] == {
        "id": upstream.id,
        "source": upstream.source,
        "type": upstream.type,
        "subject": job_id,
    }
