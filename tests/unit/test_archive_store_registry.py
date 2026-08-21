from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import riverhog_api.deps as deps
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_storage_adapter_protocol import AdapterDescriptor, StorageAdapterPort


class _Adapter:
    def descriptor(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            implementation_id="fixture.storage/v1",
            implementation_version="1.0.0",
            read_mode="immediate",
            minimum_nonfinal_part_bytes=1,
            maximum_part_bytes=1024,
            maximum_part_count=10_000,
        )


def test_composition_binds_every_capability_to_each_configured_archive_store(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    base = RuntimeConfig()
    primary = base.archive_store("archive")
    secondary = replace(primary, name="secondary", base_url="http://127.0.0.1/secondary")
    config = replace(
        base,
        archive_stores={"archive": primary, "secondary": secondary},
        archive_read_order=("secondary", "archive"),
    )
    adapters = {
        name: cast(StorageAdapterPort, cast(Any, _Adapter())) for name in config.archive_stores
    }

    registry = deps._archive_store_registry(
        config,
        adapters=adapters,  # type: ignore[arg-type]
        download_allowance=object(),  # type: ignore[arg-type]
    )

    assert registry.names == tuple(config.archive_stores)
    for name in registry.names:
        binding = registry.require(name)
        expected = adapters[name]
        assert binding.store._adapter is expected  # type: ignore[attr-defined]
        assert binding.multipart_objects._adapter is expected  # type: ignore[attr-defined]
        assert binding.immutable_objects._adapter is expected  # type: ignore[attr-defined]
        assert binding.object_ranges._adapter is expected  # type: ignore[attr-defined]
