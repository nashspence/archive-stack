from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
import riverhog_cli.main
import typer
from riverhog_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def test_app_list_matches_pipeable_list_conventions(monkeypatch) -> None:
    class FakeClient:
        @contextmanager
        def stream_apps(self, **kwargs: Any) -> Iterator[Iterator[dict[str, object]]]:
            assert kwargs == {
                "q": "review",
                "sort": "name",
                "order": "asc",
                "active": True,
            }
            yield iter(({"name": "review-station"},))

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(
        app,
        ["app", "list", "--query", "review", "--active", "--all", "--ids"],
    )

    assert result.exit_code == 0
    assert result.stdout == "review-station\n"
    human = runner.invoke(app, ["app", "list", "--query", "review", "--active", "--all"])
    structured = runner.invoke(
        app, ["app", "list", "--query", "review", "--active", "--all", "--json"]
    )
    assert human.exit_code == structured.exit_code == 0
    assert "review-station" in human.stdout
    assert json.loads(structured.stdout)["apps"][0]["name"] == "review-station"


def test_app_key_create_emits_machine_readable_one_time_token(monkeypatch) -> None:
    class FakeClient:
        def create_app_key(
            self,
            app_name: str,
            *,
            access: list[dict[str, str]],
            expires_in_seconds: int | None,
        ) -> dict[str, object]:
            assert app_name == "local"
            assert access == [
                {"permission": "catalog:read", "resource": "tag:photos"},
                {"permission": "retrieval:manage", "resource": "tag:photos"},
            ]
            assert expires_in_seconds == 2_592_000
            return {
                "id": "0123456789abcdef",
                "app": "local",
                "access": access,
                "status": "active",
                "created_at": "2026-07-18T00:00:00.000000Z",
                "expires_at": "2026-08-17T00:00:00.000000Z",
                "revoked_at": None,
                "last_used_at": None,
                "token": "rh_app_secret",
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(
        app,
        [
            "app",
            "key",
            "create",
            "local",
            "--allow",
            "catalog:read=tag:photos",
            "--allow",
            "retrieval:manage=tag:photos",
            "--expires-in",
            "30d",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["token"] == "rh_app_secret"
    human = runner.invoke(
        app,
        [
            "app",
            "key",
            "create",
            "local",
            "--allow",
            "catalog:read=tag:photos",
            "--allow",
            "retrieval:manage=tag:photos",
            "--expires-in",
            "30d",
        ],
    )
    assert human.exit_code == 0
    assert "0123456789abcdef" in human.stdout
    assert "rh_app_secret" in human.stdout


def test_app_key_list_never_requires_or_formats_plaintext(monkeypatch) -> None:
    class FakeClient:
        @contextmanager
        def stream_app_keys(
            self, app_name: str, **kwargs: Any
        ) -> Iterator[Iterator[dict[str, object]]]:
            assert app_name == "local"
            assert kwargs["active"] is False
            yield iter(({"id": "0123456789abcdef", "status": "revoked"},))

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(
        app,
        ["app", "key", "list", "local", "--inactive", "--all", "--ids"],
    )

    assert result.exit_code == 0
    assert result.stdout == "0123456789abcdef\n"
    human = runner.invoke(app, ["app", "key", "list", "local", "--inactive", "--all"])
    structured = runner.invoke(
        app, ["app", "key", "list", "local", "--inactive", "--all", "--json"]
    )
    assert human.exit_code == structured.exit_code == 0
    assert "0123456789abcdef" in human.stdout
    assert json.loads(structured.stdout)["keys"][0]["id"] == "0123456789abcdef"
    assert "token" not in structured.stdout


def test_access_and_quota_lists_match_pipeable_conventions(monkeypatch) -> None:
    class FakeClient:
        @contextmanager
        def stream_app_key_access(
            self,
            **kwargs: Any,
        ) -> Iterator[Iterator[dict[str, object]]]:
            assert kwargs == {
                "q": "photos",
                "sort": "permission",
                "order": "asc",
                "app": "local",
                "key_id": "key-one",
                "permission": None,
                "resource": "tag:photos",
                "active": True,
            }
            yield iter(
                (
                    {
                        "app": "local",
                        "key_id": "key-one",
                        "key_status": "active",
                        "permission": "catalog:read",
                        "resource": "tag:photos",
                    },
                )
            )

        @contextmanager
        def stream_download_quotas(self, **kwargs: Any) -> Iterator[Iterator[dict[str, object]]]:
            assert kwargs == {
                "q": "review",
                "sort": "remaining_bytes",
                "order": "desc",
                "app": "local",
                "active": True,
            }
            yield iter(
                (
                    {
                        "id": "key-one",
                        "app": "local",
                        "key_id": "key-one",
                        "key_status": "active",
                        "monthly_bytes": 1024,
                        "accounted_bytes": 0,
                        "reserved_bytes": 0,
                        "remaining_bytes": 1024,
                        "resets_at": "2026-09-01T00:00:00Z",
                    },
                )
            )

        def remove_app_key_access(
            self,
            app_name: str,
            key_id: str,
            *,
            permission: str,
            resource: str,
        ) -> dict[str, object]:
            assert (app_name, key_id, permission, resource) == (
                "local",
                "key-one",
                "catalog:read",
                "tag:photos",
            )
            return {"app": app_name, "key_id": key_id, "access": []}

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    access = runner.invoke(
        app,
        [
            "app",
            "key",
            "access",
            "list",
            "--app",
            "local",
            "--key",
            "key-one",
            "--resource",
            "tag:photos",
            "--active",
            "--query",
            "photos",
            "--all",
        ],
    )
    quotas = runner.invoke(
        app,
        [
            "app",
            "key",
            "quota",
            "list",
            "--query",
            "review",
            "--app",
            "local",
            "--active",
            "--sort",
            "remaining_bytes",
            "--order",
            "desc",
            "--all",
            "--ids",
        ],
    )

    assert access.exit_code == 0
    assert "local/key-one" in access.stdout
    assert "permission=catalog:read" in access.stdout
    assert "resource=tag:photos" in access.stdout
    assert quotas.exit_code == 0
    assert quotas.stdout == "key-one\n"

    access_json = runner.invoke(
        app,
        [
            "app",
            "key",
            "access",
            "list",
            "--app",
            "local",
            "--key",
            "key-one",
            "--resource",
            "tag:photos",
            "--active",
            "--query",
            "photos",
            "--all",
            "--json",
        ],
    )
    quota_human = runner.invoke(
        app,
        [
            "app",
            "key",
            "quota",
            "list",
            "--query",
            "review",
            "--app",
            "local",
            "--active",
            "--sort",
            "remaining_bytes",
            "--order",
            "desc",
            "--all",
        ],
    )
    quota_json = runner.invoke(
        app,
        [
            "app",
            "key",
            "quota",
            "list",
            "--query",
            "review",
            "--app",
            "local",
            "--active",
            "--sort",
            "remaining_bytes",
            "--order",
            "desc",
            "--all",
            "--json",
        ],
    )
    assert json.loads(access_json.stdout)["access"][0]["key_id"] == "key-one"
    assert "key-one" in quota_human.stdout
    assert json.loads(quota_json.stdout)["quotas"][0]["id"] == "key-one"

    selectors = runner.invoke(
        app,
        [
            "app",
            "key",
            "access",
            "list",
            "--app",
            "local",
            "--key",
            "key-one",
            "--resource",
            "tag:photos",
            "--active",
            "--query",
            "photos",
            "--all",
            "--selectors",
        ],
    )
    removed = runner.invoke(
        app,
        [
            "app",
            "key",
            "access",
            "remove",
            "local::key-one::catalog:read=tag:photos",
            "--json",
        ],
    )
    assert selectors.exit_code == 0
    assert selectors.stdout == "local::key-one::catalog:read=tag:photos\n"
    assert removed.exit_code == 0
    assert json.loads(removed.stdout)["access"] == []
    removed_human = runner.invoke(
        app,
        ["app", "key", "access", "remove", "local::key-one::catalog:read=tag:photos"],
    )
    assert removed_human.exit_code == 0
    assert "local" in removed_human.stdout


def test_quota_ids_require_an_application_for_actionable_identity() -> None:
    with pytest.raises(typer.BadParameter, match="--ids requires --app"):
        riverhog_cli.main.app_key_quota_list_cmd(ids=True)


def test_quota_assignment_accepts_human_binary_sizes_and_explicit_unlimited(monkeypatch) -> None:
    assigned: list[int | None] = []

    class FakeClient:
        def set_app_key_download_quota(
            self,
            app_name: str,
            key_id: str,
            *,
            monthly_bytes: int | None,
        ) -> dict[str, object]:
            assert (app_name, key_id) == ("local", "key-one")
            assigned.append(monthly_bytes)
            return {
                "app": app_name,
                "key_id": key_id,
                "key_status": "active",
                "monthly_bytes": monthly_bytes,
                "accounted_bytes": 0,
                "reserved_bytes": 0,
                "remaining_bytes": monthly_bytes,
                "resets_at": "2026-09-01T00:00:00Z",
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    finite = runner.invoke(
        app,
        ["app", "key", "quota", "set", "local", "key-one", "500GiB", "--json"],
    )
    unlimited = runner.invoke(
        app,
        ["app", "key", "quota", "set", "local", "key-one", "unlimited", "--json"],
    )

    assert finite.exit_code == 0
    assert unlimited.exit_code == 0
    finite_human = runner.invoke(app, ["app", "key", "quota", "set", "local", "key-one", "500GiB"])
    assert finite_human.exit_code == 0
    assert "local" in finite_human.stdout
    assert assigned == [500 * 1024**3, None, 500 * 1024**3]


def test_app_key_policy_mutations_have_human_json_parity(monkeypatch) -> None:
    access = [{"permission": "catalog:read", "resource": "tag:photos"}]
    quota = {
        "app": "local",
        "key_id": "key-one",
        "key_status": "active",
        "monthly_bytes": 1024,
        "accounted_bytes": 0,
        "reserved_bytes": 0,
        "remaining_bytes": 1024,
        "resets_at": "2026-09-01T00:00:00Z",
    }

    class FakeClient:
        def replace_app_key_access(
            self, app_name: str, key_id: str, *, access: list[dict[str, str]]
        ) -> dict[str, object]:
            return {"app": app_name, "key_id": key_id, "access": access}

        def add_app_key_access(
            self,
            app_name: str,
            key_id: str,
            *,
            permission: str,
            resource: str,
        ) -> dict[str, object]:
            return {
                "app": app_name,
                "key_id": key_id,
                "access": [{"permission": permission, "resource": resource}],
            }

        def revoke_app_key(self, app_name: str, key_id: str) -> dict[str, object]:
            return {
                "app": app_name,
                "id": key_id,
                "status": "revoked",
                "revoked_at": "2026-08-13T00:00:00Z",
            }

        def rotate_app_key(self, app_name: str, key_id: str) -> dict[str, object]:
            return {
                "app": app_name,
                "id": key_id,
                "access": access,
                "token": "one-time-rotated-token",
            }

        def get_download_quota(self) -> dict[str, object]:
            return dict(quota)

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    cases = (
        (
            [
                "app",
                "key",
                "access",
                "set",
                "local",
                "key-one",
                "--allow",
                "catalog:read=tag:photos",
            ],
            "key-one",
        ),
        (
            [
                "app",
                "key",
                "access",
                "add",
                "local",
                "key-one",
                "catalog:read=tag:photos",
            ],
            "key-one",
        ),
        (["app", "key", "revoke", "local", "key-one"], "key-one"),
        (["app", "key", "rotate", "local", "key-one"], "key-one"),
        (["app", "key", "quota", "show"], "key-one"),
    )
    for arguments, identity in cases:
        human = runner.invoke(app, arguments)
        structured = runner.invoke(app, [*arguments, "--json"])
        assert human.exit_code == 0, human.output
        assert structured.exit_code == 0, structured.output
        assert identity in human.stdout
        assert identity in json.dumps(json.loads(structured.stdout), sort_keys=True)
