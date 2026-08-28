from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

import gogurt.providers as provider_module
import pytest
from config_validation import ConfigError
from gogurt.cli import app
from gogurt.providers import (
    list_listener_host_providers,
    list_mounted_volume_providers,
    resolve_listener_host_provider,
    resolve_mounted_volume_provider,
)
from gogurt_core.core import (
    plan_gogurt_action,
    revalidate_gogurt_action,
    write_gogurt_marker,
)
from gogurt_core.mounts import (
    GOGURT_MOUNTED_VOLUME_PROVIDER_ENTRY_POINT_GROUP,
    GogurtRouteMarker,
    MountedMarkerObservation,
    MountedVolumeProviderBinding,
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


class FixtureMountedVolumeAccess:
    def __init__(self, markers: dict[Path, GogurtRouteMarker] | None = None) -> None:
        self.markers = markers if markers is not None else {}

    def discover(self) -> tuple[Path, ...]:
        return (_fixture_root("fixture-mount"),)

    def observe_marker(
        self,
        mount_point: Path,
    ) -> MountedMarkerObservation | None:
        marker = self.markers.get(mount_point)
        if marker is None:
            return None
        identity = sha256(
            b"fixture-mounted-marker/v1\0"
            + str(mount_point).encode("utf-8")
            + b"\0"
            + marker.format.encode("ascii")
            + b"\0"
            + marker.route.encode("ascii")
        ).hexdigest()
        return MountedMarkerObservation(marker, identity)

    def publish_marker(
        self,
        mount_point: Path,
        marker: GogurtRouteMarker,
        *,
        expected: MountedMarkerObservation | None,
    ) -> MountedMarkerObservation:
        if self.observe_marker(mount_point) != expected:
            raise ConfigError("gogurt marker changed before publication")
        self.markers[mount_point] = marker
        value = self.observe_marker(mount_point)
        assert value is not None
        return value


class InvalidDiscoveryAccess(FixtureMountedVolumeAccess):
    def discover(self) -> tuple[Path, ...]:
        return ("not-a-path",)  # type: ignore[return-value]


class InvalidObservationResultAccess(FixtureMountedVolumeAccess):
    def observe_marker(
        self,
        mount_point: Path,
    ) -> MountedMarkerObservation | None:
        return "not-an-observation"  # type: ignore[return-value]


class InvalidPublicationAccess(FixtureMountedVolumeAccess):
    def publish_marker(
        self,
        mount_point: Path,
        marker: GogurtRouteMarker,
        *,
        expected: MountedMarkerObservation | None,
    ) -> MountedMarkerObservation:
        del expected
        return MountedMarkerObservation(
            marker=GogurtRouteMarker("different"),
            identity="wrong-publication",
        )


def _fixture_root(name: str) -> Path:
    return Path.cwd().resolve() / name


def _paths() -> ListenerRuntimePaths:
    root = _fixture_root("fixture-gogurt")
    return ListenerRuntimePaths(
        state_dir=root,
        config_file=root / "listener.json",
        database_file=root / "listener.sqlite3",
        heartbeat_file=root / "heartbeat.json",
        lock_file=root / "listener.lock",
        log_file=root / "listener.log",
        stop_file=root / "stop.request",
    )


def _bindings() -> tuple[MountedVolumeProviderBinding, ListenerHostProviderBinding]:
    return (
        MountedVolumeProviderBinding(
            provider_id="external-mounted-volume-provider/v1",
            access=FixtureMountedVolumeAccess(),
        ),
        ListenerHostProviderBinding(
            provider_id="external-listener-host-provider/v1",
            paths=_paths,
            adapter=FixtureAdapter,
            executable=lambda _raw=None: _fixture_root("fixture-gogurt-executable"),
        ),
    )


def _patch_entries(monkeypatch: pytest.MonkeyPatch) -> tuple[FixtureEntryPoint, FixtureEntryPoint]:
    mount, host = _bindings()
    mount_entry = FixtureEntryPoint("external-mount", "fixture:MOUNT", mount)
    host_entry = FixtureEntryPoint("external-host", "fixture:HOST", host)
    entries = {
        GOGURT_MOUNTED_VOLUME_PROVIDER_ENTRY_POINT_GROUP: (mount_entry,),
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

    assert [item.name for item in list_mounted_volume_providers()] == ["external-mount"]
    assert [item.name for item in list_listener_host_providers()] == ["external-host"]
    assert mount.loaded is False
    assert host.loaded is False


def test_independent_external_providers_resolve_by_exact_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_entries(monkeypatch)

    mount = resolve_mounted_volume_provider("external-mount")
    host = resolve_listener_host_provider("external-host")

    assert mount.discover() == (_fixture_root("fixture-mount"),)
    assert mount.reference.provider_id == "external-mounted-volume-provider/v1"
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
        lambda *, group: (
            entries if group == GOGURT_MOUNTED_VOLUME_PROVIDER_ENTRY_POINT_GROUP else ()
        ),
    )

    with pytest.raises(ValueError, match="resolve exactly once"):
        resolve_mounted_volume_provider("missing")
    with pytest.raises(ValueError, match="resolve exactly once"):
        resolve_mounted_volume_provider("duplicate")


def test_provider_binding_and_capability_mismatches_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries: tuple[FixtureEntryPoint, ...] = (
        FixtureEntryPoint("wrong-binding", "fixture:WRONG", object()),
        FixtureEntryPoint(
            "wrong-result",
            "fixture:RESULT",
            MountedVolumeProviderBinding(
                provider_id="wrong-result/v1",
                access=InvalidDiscoveryAccess(),
            ),
        ),
        FixtureEntryPoint(
            "wrong-snapshot",
            "fixture:SNAPSHOT",
            MountedVolumeProviderBinding(
                provider_id="wrong-snapshot/v1",
                access=InvalidObservationResultAccess(),
            ),
        ),
    )
    monkeypatch.setattr(
        provider_module.importlib.metadata,
        "entry_points",
        lambda *, group: (
            entries if group == GOGURT_MOUNTED_VOLUME_PROVIDER_ENTRY_POINT_GROUP else ()
        ),
    )

    with pytest.raises(TypeError, match="invalid binding"):
        resolve_mounted_volume_provider("wrong-binding")
    with pytest.raises(ConfigError, match="invalid mount paths"):
        resolve_mounted_volume_provider("wrong-result").discover()
    with pytest.raises(ConfigError, match="invalid marker observation"):
        resolve_mounted_volume_provider("wrong-snapshot").observe_marker(
            _fixture_root("fixture-mount")
        )


def test_persisted_provider_identity_is_verified_after_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_entries(monkeypatch)
    expected = GogurtProviderReference(
        kind="mounted-volume",
        name="external-mount",
        provider_id="different-mounted-volume-provider/v1",
    )

    with pytest.raises(ValueError, match="differs from persisted identity"):
        resolve_mounted_volume_provider("external-mount", expected=expected)


def test_external_provider_owns_marker_custody_and_restart_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shared_markers: dict[Path, GogurtRouteMarker] = {}
    volume_root = tmp_path / "provider-owned-volume-that-is-not-a-local-directory"
    config = tmp_path / "routes.yaml"
    config.write_text(
        "schema_version: 1\n"
        "kind: gogurt.routes\n"
        "routes:\n"
        "  camera:\n"
        "    command:\n"
        '      - "{python}"\n'
        "      - -c\n"
        "      - pass\n"
        '      - "{mount_point}"\n',
        encoding="utf-8",
    )

    def install_access(access: FixtureMountedVolumeAccess) -> None:
        entry = FixtureEntryPoint(
            "external-volume",
            "external:VOLUME",
            MountedVolumeProviderBinding(
                provider_id="external-volume-provider/v1",
                access=access,
            ),
        )
        monkeypatch.setattr(
            provider_module.importlib.metadata,
            "entry_points",
            lambda *, group: (
                (entry,) if group == GOGURT_MOUNTED_VOLUME_PROVIDER_ENTRY_POINT_GROUP else ()
            ),
        )

    install_access(FixtureMountedVolumeAccess(shared_markers))
    provider = resolve_mounted_volume_provider("external-volume")
    observation = write_gogurt_marker(config, "camera", volume_root, provider=provider)
    plan = plan_gogurt_action(config, volume_root, provider=provider)

    assert observation.marker == GogurtRouteMarker("camera")
    assert not volume_root.exists()
    assert shared_markers[volume_root.resolve()] == GogurtRouteMarker("camera")
    assert plan["mounted_volume_provider"] == provider.reference.as_dict()

    install_access(FixtureMountedVolumeAccess(shared_markers))
    restarted = resolve_mounted_volume_provider(
        "external-volume",
        expected=provider.reference,
    )
    assert revalidate_gogurt_action(plan, provider=restarted) == plan["command"]

    shared_markers[volume_root.resolve()] = GogurtRouteMarker("otherx")
    with pytest.raises(ConfigError, match="changed before action execution"):
        revalidate_gogurt_action(plan, provider=restarted)


def test_action_plan_rejects_a_different_provider_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_entries(monkeypatch)
    provider = resolve_mounted_volume_provider("external-mount")
    root = _fixture_root("fixture-mount")
    provider.publish_marker(root, GogurtRouteMarker("camera"), expected=None)
    config = tmp_path / "routes.yaml"
    config.write_text(
        "schema_version: 1\nkind: gogurt.routes\nroutes:\n"
        '  camera:\n    command:\n      - "{python}"\n      - -c\n'
        '      - pass\n      - "{mount_point}"\n',
        encoding="utf-8",
    )
    plan = plan_gogurt_action(config, root, provider=provider)
    wrong = replace(
        resolve_mounted_volume_provider("external-mount"),
        reference=GogurtProviderReference(
            kind="mounted-volume",
            name="other-provider",
            provider_id=provider.reference.provider_id,
        ),
    )

    with pytest.raises(ConfigError, match="provider identity changed"):
        revalidate_gogurt_action(plan, provider=wrong)


def test_core_revalidates_provider_publication_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    access = InvalidPublicationAccess()
    entry = FixtureEntryPoint(
        "external-volume",
        "external:VOLUME",
        MountedVolumeProviderBinding("external-volume/v1", access),
    )
    monkeypatch.setattr(
        provider_module.importlib.metadata,
        "entry_points",
        lambda *, group: (
            (entry,) if group == GOGURT_MOUNTED_VOLUME_PROVIDER_ENTRY_POINT_GROUP else ()
        ),
    )
    provider = resolve_mounted_volume_provider("external-volume")
    config = tmp_path / "routes.yaml"
    config.write_text(
        "schema_version: 1\nkind: gogurt.routes\nroutes:\n"
        '  camera:\n    command:\n      - "{python}"\n      - -c\n'
        '      - pass\n      - "{mount_point}"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="did not publish the requested logical marker"):
        write_gogurt_marker(config, "camera", tmp_path / "volume", provider=provider)


def test_installed_external_distribution_composes_without_workspace_registration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "external_gogurt.py").write_text(
        "\n".join(
            (
                "from pathlib import Path",
                "from gogurt_core.mounts import ("
                "GogurtRouteMarker, MountedMarkerObservation, MountedVolumeProviderBinding)",
                "from gogurt_listener_runtime.platform import (",
                "    ListenerHostProviderBinding, ListenerRuntimePaths, NativeListenerStatus)",
                "class VolumeAccess:",
                "    markers = {}",
                "    def discover(self): return (Path.cwd().resolve() / 'external-mount',)",
                "    def observe_marker(self, mount_point):",
                "        marker = self.markers.get(mount_point)",
                "        if marker is None: return None",
                "        return MountedMarkerObservation(marker, marker.route)",
                "    def publish_marker(self, mount_point, marker, *, expected):",
                "        if self.observe_marker(mount_point) != expected:",
                "            raise RuntimeError('marker changed')",
                "        self.markers[mount_point] = marker",
                "        return self.observe_marker(mount_point)",
                "class Adapter:",
                "    def register(self, paths, command): pass",
                "    def status(self, paths):",
                "        return NativeListenerStatus(installed=True, enabled=True, running=True)",
                "    def start(self, paths): pass",
                "    def stop(self, paths): pass",
                "    def unregister(self, paths): pass",
                "    def process_is_running(self, pid): return False",
                "def paths():",
                "    root = Path.cwd().resolve() / 'external-gogurt'",
                "    return ListenerRuntimePaths(root, root/'config', root/'db', root/'heartbeat',",
                "                                root/'lock', root/'log', root/'stop')",
                "VOLUME = MountedVolumeProviderBinding(",
                "    'external-freebsd-mounted-volume/v1', VolumeAccess())",
                "HOST = ListenerHostProviderBinding(",
                "    'external-freebsd-host/v1', paths, Adapter,",
                "    lambda raw=None: Path.cwd().resolve() / 'external-gogurt-executable')",
            )
        ),
        encoding="utf-8",
    )
    distributions = (
        (
            "external_gogurt_mounted_volume-1.0.dist-info",
            "external-gogurt-mounted-volume",
            "gogurt.mounted-volume-providers",
            "external_gogurt:VOLUME",
        ),
        (
            "external_gogurt_listener_host-1.0.dist-info",
            "external-gogurt-listener-host",
            "gogurt.listener-host-providers",
            "external_gogurt:HOST",
        ),
    )
    for directory, distribution, group, entry_point in distributions:
        metadata = tmp_path / directory
        metadata.mkdir()
        (metadata / "METADATA").write_text(
            f"Metadata-Version: 2.4\nName: {distribution}\nVersion: 1.0\n",
            encoding="utf-8",
        )
        (metadata / "entry_points.txt").write_text(
            f"[{group}]\nexternal-freebsd = {entry_point}\n",
            encoding="utf-8",
        )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    mount = resolve_mounted_volume_provider("external-freebsd")
    host = resolve_listener_host_provider("external-freebsd")

    assert mount.metadata.distribution == "external-gogurt-mounted-volume"
    assert mount.discover() == (_fixture_root("external-mount"),)
    assert host.metadata.distribution == "external-gogurt-listener-host"
    assert host.metadata.version == "1.0"
    assert host.paths().state_dir == _fixture_root("external-gogurt")

    listener_config = ListenerConfig(
        executable=tmp_path / "gogurt",
        routes_file=tmp_path / "routes.yaml",
        actions_dir=None,
        interval_seconds=2,
        state_dir=tmp_path,
        mounted_volume_provider=mount.reference,
        listener_host_provider=host.reference,
    )
    config_path = tmp_path / "listener.json"
    listener_config.write(config_path)
    del mount, host
    sys.modules.pop("external_gogurt", None)
    importlib.invalidate_caches()

    restored = ListenerConfig.read(config_path)
    assert resolve_mounted_volume_provider(
        restored.mounted_volume_provider.name,
        expected=restored.mounted_volume_provider,
    ).discover() == (_fixture_root("external-mount"),)
    assert resolve_listener_host_provider(
        restored.listener_host_provider.name,
        expected=restored.listener_host_provider,
    ).paths().state_dir == _fixture_root("external-gogurt")


def test_provider_cli_has_human_json_and_ids_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_entries(monkeypatch)
    runner = CliRunner()

    for kind, name in (
        ("mounted-volume", "external-mount"),
        ("listener-host", "external-host"),
    ):
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
        env={"GOGURT_MOUNTED_VOLUME_PROVIDER": "external-mount"},
    )
    status = runner.invoke(
        app,
        ["listener", "status", "--json"],
        env={"GOGURT_LISTENER_HOST_PROVIDER": "external-host"},
    )
    assert mounts.exit_code == 0
    assert json.loads(mounts.stdout) == [str(_fixture_root("fixture-mount"))]
    assert status.exit_code == 0
    assert json.loads(status.stdout)["listener_host_provider"]["name"] == "external-host"
