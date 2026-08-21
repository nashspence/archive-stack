from __future__ import annotations

from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import riverhog_aws_storage_adapter.app as aws_app
import riverhog_aws_storage_adapter.recovery as aws_recovery
import riverhog_backblaze_storage_adapter.app as backblaze_app
import riverhog_backblaze_storage_adapter.recovery as backblaze_recovery
import riverhog_garage_storage_adapter.app as garage_app
import riverhog_garage_storage_adapter.recovery as garage_recovery
from riverhog_aws_storage_adapter.config import AwsStorageAdapterConfig
from riverhog_backblaze_storage_adapter.config import BackblazeStorageAdapterConfig
from riverhog_backblaze_storage_adapter.driver import BackblazeStorageDriver
from riverhog_garage_storage_adapter.config import GarageStorageAdapterConfig
from riverhog_garage_storage_adapter.driver import GarageStorageDriver

_ENTRYPOINTS = (
    (
        aws_app,
        "AwsStorageDriver",
        "RIVERHOG_AWS_STORAGE_ADAPTER_HOST",
        "RIVERHOG_AWS_STORAGE_ADAPTER_PORT",
        "RIVERHOG_AWS_STORAGE_ADAPTER_SOURCE_REVISION",
    ),
    (
        backblaze_app,
        "BackblazeStorageDriver",
        "RIVERHOG_BACKBLAZE_STORAGE_ADAPTER_HOST",
        "RIVERHOG_BACKBLAZE_STORAGE_ADAPTER_PORT",
        "RIVERHOG_BACKBLAZE_STORAGE_ADAPTER_SOURCE_REVISION",
    ),
    (
        garage_app,
        "GarageStorageDriver",
        "RIVERHOG_GARAGE_STORAGE_ADAPTER_HOST",
        "RIVERHOG_GARAGE_STORAGE_ADAPTER_PORT",
        "RIVERHOG_GARAGE_STORAGE_ADAPTER_SOURCE_REVISION",
    ),
)


@pytest.mark.parametrize(
    ("module", "_driver", "host_env", "port_env", "_revision_env"),
    _ENTRYPOINTS,
)
def test_adapter_listener_environment_reaches_the_process_parser(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    _driver: str,
    host_env: str,
    port_env: str,
    _revision_env: str,
) -> None:
    monkeypatch.setenv(host_env, "adapter.internal")
    monkeypatch.setenv(port_env, "9187")

    args = module._parser().parse_args([])  # type: ignore[attr-defined]

    assert args.host == "adapter.internal"
    assert args.port == 9187


@pytest.mark.parametrize(
    ("module", "driver_name", "_host_env", "_port_env", "revision_env"),
    _ENTRYPOINTS,
)
def test_adapter_source_revision_is_bound_into_runtime_evidence(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    driver_name: str,
    _host_env: str,
    _port_env: str,
    revision_env: str,
) -> None:
    class _Driver:
        def __init__(
            self,
            _config: object,
            *,
            implementation_version: str,
            source_revision: str,
        ) -> None:
            self.implementation_version = implementation_version
            self.source_revision = source_revision

    monkeypatch.setenv(revision_env, "abc123")
    monkeypatch.setattr(module, driver_name, _Driver)

    service = module.build_service(SimpleNamespace(state_root=tmp_path))  # type: ignore[attr-defined,arg-type]

    assert service._driver.source_revision == "abc123"  # type: ignore[attr-defined]
    assert service._driver.implementation_version  # type: ignore[attr-defined]


def test_provider_adapter_configuration_owns_and_normalizes_its_target_root(
    tmp_path: Path,
) -> None:
    aws = AwsStorageAdapterConfig(
        bucket="archive",
        region="us-west-2",
        profile_id="riverhog.aws-archive/v1",
        egress_accounting_id="aws-egress",
        token_file=tmp_path / "token",
        state_root=tmp_path / "aws-state",
        prefix="/qualification/run/",
    )
    backblaze = BackblazeStorageAdapterConfig(
        endpoint_url="https://s3.example.invalid",
        region="test",
        bucket="archive",
        access_key_id="key",
        secret_access_key="secret",
        profile_id="riverhog.backblaze-archive/v1",
        egress_accounting_id="backblaze-egress",
        token_file=tmp_path / "token",
        state_root=tmp_path / "backblaze-state",
        prefix="/qualification/run/",
    )
    garage = GarageStorageAdapterConfig(
        endpoint_url="http://garage:3900",
        region="garage",
        bucket="archive",
        access_key_id="key",
        secret_access_key="secret",
        profile_id="riverhog.garage-development/v1",
        egress_accounting_id="garage-development",
        token_file=tmp_path / "token",
        state_root=tmp_path / "garage-state",
        prefix="/qualification/run/",
    )

    assert {aws.prefix, backblaze.prefix, garage.prefix} == {"qualification/run"}
    assert backblaze.endpoint_url == "https://s3.example.invalid"
    assert backblaze.region == "test"
    assert backblaze.bucket == "archive"
    assert backblaze.access_key_id == "key"
    assert backblaze.secret_access_key == "secret"
    assert backblaze.profile_id == "riverhog.backblaze-archive/v1"
    assert backblaze.egress_accounting_id == "backblaze-egress"
    assert backblaze.token_file == tmp_path / "token"
    assert backblaze.state_root == tmp_path / "backblaze-state"
    assert backblaze.max_pool_connections == 32
    assert garage.endpoint_url == "http://garage:3900"
    assert garage.region == "garage"
    assert garage.bucket == "archive"
    assert garage.access_key_id == "key"
    assert garage.secret_access_key == "secret"
    assert garage.profile_id == "riverhog.garage-development/v1"
    assert garage.egress_accounting_id == "garage-development"
    assert garage.token_file == tmp_path / "token"
    assert garage.state_root == tmp_path / "garage-state"
    assert garage.max_pool_connections == 32


@pytest.mark.parametrize(
    "prefix",
    ("../outside", "owned/../outside", "owned//outside", "owned\\outside"),
)
def test_provider_adapter_target_roots_reject_noncanonical_paths(
    tmp_path: Path,
    prefix: str,
) -> None:
    with pytest.raises(ValueError, match="object path"):
        GarageStorageAdapterConfig(
            endpoint_url="http://garage:3900",
            region="garage",
            bucket="archive",
            access_key_id="key",
            secret_access_key="secret",
            profile_id="riverhog.garage-development/v1",
            egress_accounting_id="garage-development",
            token_file=tmp_path / "token",
            state_root=tmp_path / "state",
            prefix=prefix,
        )


def test_backblaze_and_garage_descriptors_share_the_public_contract(
    tmp_path: Path,
) -> None:
    backblaze = BackblazeStorageDriver(
        BackblazeStorageAdapterConfig(
            endpoint_url="https://s3.example.invalid",
            region="test",
            bucket="archive",
            access_key_id="key",
            secret_access_key="secret",
            profile_id="riverhog.backblaze-archive/v1",
            egress_accounting_id="backblaze-egress",
            token_file=tmp_path / "token",
            state_root=tmp_path / "backblaze-state",
        ),
        implementation_version="1",
        source_revision="fixture",
        client=object(),
    ).descriptor()
    garage = GarageStorageDriver(
        GarageStorageAdapterConfig(
            endpoint_url="http://garage:3900",
            region="garage",
            bucket="archive",
            access_key_id="key",
            secret_access_key="secret",
            profile_id="riverhog.garage-development/v1",
            egress_accounting_id="garage-development",
            token_file=tmp_path / "token",
            state_root=tmp_path / "garage-state",
        ),
        implementation_version="1",
        source_revision="fixture",
        client=object(),
    ).descriptor()

    assert backblaze.protocol == garage.protocol == "riverhog-storage-adapter/v1"
    assert backblaze.profile.read_mode == garage.profile.read_mode == "immediate"
    assert backblaze.implementation_id == "riverhog.backblaze-storage-adapter/v1"
    assert garage.implementation_id == "riverhog.garage-storage-adapter/v1"


@pytest.mark.parametrize(
    ("module", "driver_name"),
    (
        (aws_recovery, "AwsStorageDriver"),
        (backblaze_recovery, "BackblazeStorageDriver"),
        (garage_recovery, "GarageStorageDriver"),
    ),
)
def test_adapter_recovery_commands_use_the_shared_configured_root_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    driver_name: str,
) -> None:
    configured = object()
    source = object()
    observed: dict[str, object] = {}

    monkeypatch.setattr(module, driver_name, lambda *_args, **_kwargs: source)
    config_class = next(
        value for name, value in vars(module).items() if name.endswith("StorageAdapterConfig")
    )
    monkeypatch.setattr(config_class, "from_env", lambda: configured)

    def run(factory, *, prog: str, version: str, argv: list[str]) -> int:
        observed.update(source=factory(), prog=prog, version=version, argv=argv)
        return 0

    monkeypatch.setattr(module, "recovery_export_main", run)
    destination = tmp_path / "export"

    assert module.main([str(destination)]) == 0  # type: ignore[attr-defined]
    assert observed["source"] is source
    assert observed["argv"] == [str(destination)]
    assert str(observed["prog"]).endswith("-export")
    assert observed["version"]
