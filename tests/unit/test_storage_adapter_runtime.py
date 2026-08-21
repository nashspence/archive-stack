from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from riverhog_core.catalog_db import initialize_db
from riverhog_core.runtime_config import DEV_STORAGE_PROFILE, RuntimeConfig
from riverhog_core.services.archive_stores import SqlAlchemyArchiveStoreService
from riverhog_core.stores.storage_adapter_object_store import StorageAdapterRuntime
from riverhog_storage_adapter_protocol import (
    StorageAdapterDescriptor,
    StorageAdapterDescriptorPayload,
    StorageProfile,
    StorageProfilePayload,
)
from riverhog_storage_adapter_support import StorageAdapterClient

from tests.unit.db_helpers import sqlite_url


class _DescriptorClient:
    def __init__(self, descriptor: StorageAdapterDescriptor) -> None:
        self.current = descriptor
        self.calls = 0

    def descriptor(self) -> StorageAdapterDescriptor:
        self.calls += 1
        return self.current


def _descriptor(
    implementation_id: str,
    *,
    profile: StorageProfile = DEV_STORAGE_PROFILE,
) -> StorageAdapterDescriptor:
    return StorageAdapterDescriptor.seal(
        StorageAdapterDescriptorPayload(
            implementation_id=implementation_id,
            implementation_version="1",
            source_revision="fixture",
            profile=profile,
            minimum_nonfinal_part_bytes=1,
            maximum_part_bytes=1024,
            maximum_part_count=10,
        )
    )


def _runtime(config: RuntimeConfig, client: _DescriptorClient) -> StorageAdapterRuntime:
    return StorageAdapterRuntime.connect(
        replace(
            config.storage_adapter("archive"),
            expected_implementation_id=None,
        ),
        client=cast(StorageAdapterClient, client),
    )


def test_runtime_refresh_accepts_compatible_implementation_substitution() -> None:
    config = RuntimeConfig()
    first = _descriptor("riverhog.garage-storage-adapter/v1")
    replacement = _descriptor("fixture.compatible-storage-adapter/v1")
    client = _DescriptorClient(first)
    runtime = _runtime(config, client)

    assert runtime.descriptor == first
    client.current = replacement
    assert runtime.descriptor == first
    assert runtime.refresh_descriptor() == replacement
    assert runtime.descriptor == replacement
    assert client.calls == 2


def test_runtime_refresh_rejects_a_different_stable_profile() -> None:
    config = RuntimeConfig()
    client = _DescriptorClient(_descriptor("riverhog.garage-storage-adapter/v1"))
    runtime = _runtime(config, client)
    assert runtime.descriptor == client.current
    incompatible_profile = StorageProfile.seal(
        StorageProfilePayload(
            profile_id="fixture.incompatible/v1",
            read_mode="immediate",
            egress_accounting_id="different",
        )
    )
    client.current = _descriptor(
        "fixture.incompatible-storage-adapter/v1",
        profile=incompatible_profile,
    )

    with pytest.raises(ValueError, match="profile ID differs"):
        runtime.refresh_descriptor()


def test_runtime_refresh_honors_an_explicit_implementation_readiness_pin() -> None:
    config = RuntimeConfig()
    first = _descriptor("riverhog.garage-storage-adapter/v1")
    client = _DescriptorClient(first)
    runtime = StorageAdapterRuntime.connect(
        config.storage_adapter("archive"),
        client=cast(StorageAdapterClient, client),
    )
    assert runtime.descriptor == first
    client.current = _descriptor("fixture.compatible-storage-adapter/v1")

    with pytest.raises(ValueError, match="implementation differs"):
        runtime.refresh_descriptor()


def test_archive_store_readiness_refreshes_current_runtime_evidence(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    base = RuntimeConfig(database_url=database_url)
    store = replace(base.archive_store("archive"), name="deep")
    config = replace(
        base,
        archive_stores={"deep": store},
        archive_write_store="deep",
        archive_read_order=("deep",),
    )
    initialize_db(database_url)
    first = _descriptor("riverhog.garage-storage-adapter/v1")
    replacement = _descriptor("fixture.compatible-storage-adapter/v1")
    client = _DescriptorClient(first)
    runtime = _runtime(config, client)
    service = SqlAlchemyArchiveStoreService(
        config,
        adapter_runtimes={"archive": runtime},
    )

    assert service.get("deep").adapter_implementation_id == first.implementation_id
    client.current = replacement
    summary = service.get("deep")

    assert summary.adapter_status == "ready"
    assert summary.adapter_implementation_id == replacement.implementation_id
    assert summary.adapter_runtime_descriptor_sha256 == (replacement.runtime_descriptor_sha256)
