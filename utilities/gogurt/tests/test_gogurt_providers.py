from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import gogurt.providers as provider_module
import pytest
from gogurt.cli import app
from gogurt.providers import (
    list_listener_host_providers,
    list_mount_providers,
    resolve_listener_host_provider,
    resolve_mount_provider,
)
from gogurt_core.mounts import (
    GOGURT_MOUNT_PROVIDER_ENTRY_POINT_GROUP,
    MountProviderBinding,
)
from gogurt_core.providers import GogurtProviderReference
from gogurt_listener_runtime.listener import ListenerConfig
from gogurt_listener_runtime.platform import (
    GOGURT_LISTENER_HOST_PROVIDER_ENTRY_POINT_GROUP,
    ListenerHostProviderBinding,
    ListenerRuntimePaths,
    NativeListenerStatus,
)
from typer.testing import CliRunner


@dataclass
class FixtureEntryPoint:
    name: str
    value: str
    binding: object
    loaded: bool = False
    dist: Any = None

    def load(self) -> object:
        self.loaded = True
        return self.binding


class FixtureAdapter:
    def register(self, _paths: ListenerRuntimePaths, _command: tuple[str, ...]) -> None:
        return None

    def status(self, _paths: ListenerRuntimePaths) -> NativeListenerStatus:
        return NativeListenerStatus(installed=True, enabled=True, running=True)

    def start(self, _paths: ListenerRuntimePaths) -> None:
        return None

    def stop(self, _paths: ListenerRuntimePaths) -> None:
        return None

    def unregister(self, _paths: ListenerRuntimePaths) -> None:
        return None

    def process_is_running(self, _pid: int) -> bool:
        return False


def _paths() -> ListenerRuntimePaths:
    root = Path("/fixture/gogurt")
    return ListenerRuntimePaths(
        state_dir=root,
        config_file=root / "listener.json",
        database_file=root / "listener.sqlite3",
        heartbeat_file=root / "heartbeat.json",
        lock_file=root / "listener.lock",
        log_file=root / "listener.log",
        stop_file=root / "stop.request",
    )


def _bindings() -> tuple[MountProviderBinding, ListenerHostProviderBinding]:
    return (
        MountProviderBinding(
            provider_id="external-mount-provider/v1",
            discover=lambda: (Path("/fixture/mount"),),
        ),
        ListenerHostProviderBinding(
            provider_id="external-listener-host-provider/v1",
            paths=_paths,
            adapter=FixtureAdapter,
            executable=lambda _raw=None: Path("/fixture/gogurt"),
        ),
    )


def _patch_entries(monkeypatch: pytest.MonkeyPatch) -> tuple[FixtureEntryPoint, FixtureEntryPoint]:
    mount, host = _bindings()
    mount_entry = FixtureEntryPoint("external-mount", "fixture:MOUNT", mount)
    host_entry = FixtureEntryPoint("external-host", "fixture:HOST", host)
    entries = {
        GOGURT_MOUNT_PROVIDER_ENTRY_POINT_GROUP: (mount_entry,),
        GOGURT_LISTENER_HOST_PROVIDER_ENTRY_POINT_GROUP: (host_entry,),
    }
    monkeypatch.setattr(
        provider_module.importlib.metadata,
        "entry_points",
        lambda *, group: entries.get(group, ()),
    )
    return mount_entry, host_entry


def test_provider_listing_is_safe_and_does_not_load_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount, host = _patch_entries(monkeypatch)

    assert [item.name for item in list_mount_providers()] == ["external-mount"]
    assert [item.name for item in list_listener_host_providers()] == ["external-host"]
    assert mount.loaded is False
    assert host.loaded is False


def test_independent_external_providers_resolve_by_exact_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_entries(monkeypatch)

    mount = resolve_mount_provider("external-mount")
    host = resolve_listener_host_provider("external-host")

    assert mount.discover() == (Path("/fixture/mount"),)
    assert mount.reference.provider_id == "external-mount-provider/v1"
    assert host.paths() == _paths()
    assert host.reference.provider_id == "external-listener-host-provider/v1"


def test_provider_resolution_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount, _host = _bindings()
    entries = (
        FixtureEntryPoint("duplicate", "first:MOUNT", mount),
        FixtureEntryPoint("duplicate", "second:MOUNT", mount),
    )
    monkeypatch.setattr(
        provider_module.importlib.metadata,
        "entry_points",
        lambda *, group: entries if group == GOGURT_MOUNT_PROVIDER_ENTRY_POINT_GROUP else (),
    )

    with pytest.raises(ValueError, match="resolve exactly once"):
        resolve_mount_provider("missing")
    with pytest.raises(ValueError, match="resolve exactly once"):
        resolve_mount_provider("duplicate")


def test_provider_binding_and_capability_mismatches_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries: tuple[FixtureEntryPoint, ...] = (
        FixtureEntryPoint("wrong-binding", "fixture:WRONG", object()),
        FixtureEntryPoint(
            "wrong-result",
            "fixture:RESULT",
            MountProviderBinding(
                provider_id="wrong-result/v1",
                discover=lambda: ("not-a-path",),  # type: ignore[arg-type,return-value]
            ),
        ),
    )
    monkeypatch.setattr(
        provider_module.importlib.metadata,
        "entry_points",
        lambda *, group: entries if group == GOGURT_MOUNT_PROVIDER_ENTRY_POINT_GROUP else (),
    )

    with pytest.raises(TypeError, match="invalid binding"):
        resolve_mount_provider("wrong-binding")
    with pytest.raises(TypeError, match="invalid mount paths"):
        resolve_mount_provider("wrong-result").discover()


def test_persisted_provider_identity_is_verified_after_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_entries(monkeypatch)
    expected = GogurtProviderReference(
        kind="mount",
        name="external-mount",
        provider_id="different-mount-provider/v1",
    )

    with pytest.raises(ValueError, match="differs from persisted identity"):
        resolve_mount_provider("external-mount", expected=expected)


def test_installed_external_distribution_composes_without_workspace_registration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "external_gogurt.py").write_text(
        "\n".join(
            (
                "from pathlib import Path",
                "from gogurt_core.mounts import MountProviderBinding",
                "from gogurt_listener_runtime.platform import (",
                "    ListenerHostProviderBinding, ListenerRuntimePaths, NativeListenerStatus)",
                "class Adapter:",
                "    def register(self, paths, command): pass",
                "    def status(self, paths):",
                "        return NativeListenerStatus(installed=True, enabled=True, running=True)",
                "    def start(self, paths): pass",
                "    def stop(self, paths): pass",
                "    def unregister(self, paths): pass",
                "    def process_is_running(self, pid): return False",
                "def paths():",
                "    root = Path('/external/gogurt')",
                "    return ListenerRuntimePaths(root, root/'config', root/'db', root/'heartbeat',",
                "                                root/'lock', root/'log', root/'stop')",
                "MOUNT = MountProviderBinding(",
                "    'external-freebsd-mount/v1', lambda: (Path('/mnt'),))",
                "HOST = ListenerHostProviderBinding(",
                "    'external-freebsd-host/v1', paths, Adapter,",
                "    lambda raw=None: Path('/bin/gogurt'))",
            )
        ),
        encoding="utf-8",
    )
    metadata = tmp_path / "external_gogurt-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: external-gogurt\nVersion: 1.0\n",
        encoding="utf-8",
    )
    (metadata / "entry_points.txt").write_text(
        "\n".join(
            (
                "[gogurt.mount-providers]",
                "external-freebsd = external_gogurt:MOUNT",
                "[gogurt.listener-host-providers]",
                "external-freebsd = external_gogurt:HOST",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    mount = resolve_mount_provider("external-freebsd")
    host = resolve_listener_host_provider("external-freebsd")

    assert mount.metadata.distribution == "external-gogurt"
    assert mount.discover() == (Path("/mnt"),)
    assert host.metadata.version == "1.0"
    assert host.paths().state_dir == Path("/external/gogurt")

    listener_config = ListenerConfig(
        executable=Path("/external/gogurt"),
        routes_file=tmp_path / "routes.yaml",
        actions_dir=None,
        marker_name=".gogurt",
        interval_seconds=2,
        state_dir=tmp_path,
        product_version="1.0",
        mount_provider=mount.reference,
        listener_host_provider=host.reference,
    )
    config_path = tmp_path / "listener.json"
    listener_config.write(config_path)
    del mount, host
    sys.modules.pop("external_gogurt", None)
    importlib.invalidate_caches()

    restored = ListenerConfig.read(config_path, product_version="1.0")
    assert resolve_mount_provider(
        restored.mount_provider.name,
        expected=restored.mount_provider,
    ).discover() == (Path("/mnt"),)
    assert resolve_listener_host_provider(
        restored.listener_host_provider.name,
        expected=restored.listener_host_provider,
    ).paths().state_dir == Path("/external/gogurt")


def test_provider_cli_has_human_json_and_ids_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_entries(monkeypatch)
    runner = CliRunner()

    for kind, name in (("mount", "external-mount"), ("listener-host", "external-host")):
        listed = runner.invoke(app, ["provider", kind, "list", "--json"])
        ids = runner.invoke(app, ["provider", kind, "list", "--ids"])
        shown = runner.invoke(app, ["provider", kind, "show", name, "--json"])
        human = runner.invoke(app, ["provider", kind, "show", name])

        assert listed.exit_code == 0
        assert [item["name"] for item in json.loads(listed.stdout)["providers"]] == [name]
        assert ids.stdout == f"{name}\n"
        assert shown.exit_code == 0
        assert json.loads(shown.stdout)["name"] == name
        assert name in human.stdout

    mounts = runner.invoke(
        app,
        ["mounts", "--json"],
        env={"GOGURT_MOUNT_PROVIDER": "external-mount"},
    )
    status = runner.invoke(
        app,
        ["listener", "status", "--json"],
        env={"GOGURT_LISTENER_HOST_PROVIDER": "external-host"},
    )
    assert mounts.exit_code == 0
    assert json.loads(mounts.stdout) == ["/fixture/mount"]
    assert status.exit_code == 0
    assert json.loads(status.stdout)["listener_host_provider"]["name"] == "external-host"
