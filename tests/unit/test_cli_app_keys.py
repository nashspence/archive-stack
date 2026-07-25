from __future__ import annotations

import json
from typing import Any

import riverhog_cli.main
from riverhog_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def test_app_list_matches_pipeable_list_conventions(monkeypatch) -> None:
    class FakeClient:
        def list_apps(self, **kwargs: Any) -> dict[str, object]:
            assert kwargs == {
                "page": 1,
                "per_page": 25,
                "q": "review",
                "sort": "name",
                "order": "asc",
                "active": True,
                "all_items": True,
            }
            return {
                "page": 1,
                "per_page": 1,
                "total": 1,
                "pages": 1,
                "apps": [{"name": "review-station"}],
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(
        app,
        ["app", "list", "--query", "review", "--active", "--all", "--ids"],
    )

    assert result.exit_code == 0
    assert result.stdout == "review-station\n"


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
                {"permission": "catalog:read", "resource": "slug:photos"},
                {"permission": "retrieval:manage", "resource": "slug:photos"},
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
            "catalog:read=slug:photos",
            "--allow",
            "retrieval:manage=slug:photos",
            "--expires-in",
            "30d",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["token"] == "rh_app_secret"


def test_app_key_list_never_requires_or_formats_plaintext(monkeypatch) -> None:
    class FakeClient:
        def list_app_keys(self, app_name: str, **kwargs: Any) -> dict[str, object]:
            assert app_name == "local"
            assert kwargs["active"] is False
            return {
                "app": "local",
                "page": 1,
                "per_page": 1,
                "total": 1,
                "pages": 1,
                "keys": [{"id": "0123456789abcdef", "status": "revoked"}],
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    result = runner.invoke(
        app,
        ["app", "key", "list", "local", "--inactive", "--all", "--ids"],
    )

    assert result.exit_code == 0
    assert result.stdout == "0123456789abcdef\n"


def test_access_and_quota_lists_match_pipeable_conventions(monkeypatch) -> None:
    class FakeClient:
        def list_app_key_access(
            self,
            app_name: str,
            key_id: str,
            **kwargs: Any,
        ) -> dict[str, object]:
            assert (app_name, key_id) == ("local", "key-one")
            assert kwargs == {
                "page": 1,
                "per_page": 25,
                "q": "photos",
                "sort": "permission",
                "order": "asc",
                "all_items": True,
            }
            return {
                "app": "local",
                "key_id": "key-one",
                "page": 1,
                "per_page": 1,
                "total": 1,
                "pages": 1,
                "access": [
                    {
                        "id": "catalog:read=slug:photos",
                        "permission": "catalog:read",
                        "resource": "slug:photos",
                    }
                ],
            }

        def list_download_quotas(self, **kwargs: Any) -> dict[str, object]:
            assert kwargs == {
                "page": 1,
                "per_page": 25,
                "q": "review",
                "sort": "remaining_bytes",
                "order": "desc",
                "app": "local",
                "active": True,
                "all_items": True,
            }
            return {
                "page": 1,
                "per_page": 1,
                "total": 1,
                "pages": 1,
                "quotas": [{"id": "key-one"}],
            }

    monkeypatch.setattr(riverhog_cli.main, "client", FakeClient)

    access = runner.invoke(
        app,
        [
            "app",
            "key",
            "access",
            "list",
            "local",
            "key-one",
            "--query",
            "photos",
            "--all",
            "--ids",
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
    assert access.stdout == "catalog:read=slug:photos\n"
    assert quotas.exit_code == 0
    assert quotas.stdout == "key-one\n"


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
            return {"monthly_bytes": monthly_bytes}

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
    assert assigned == [500 * 1024**3, None]
