from __future__ import annotations

from dataclasses import replace

import riverhog_api.deps as deps
from riverhog_core.runtime_config import RuntimeConfig


class _ArchiveAdapter:
    def __init__(
        self,
        _config: RuntimeConfig,
        store: object,
        runtime: object,
        **_kwargs: object,
    ) -> None:
        self.store_config = store
        self.runtime = runtime


class _ObjectAdapter:
    def __init__(self, runtime: object) -> None:
        self.runtime = runtime


def test_composition_binds_every_capability_to_each_configured_archive_store(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    base = RuntimeConfig()
    primary = base.archive_store("archive")
    secondary = replace(primary, name="secondary")
    config = replace(
        base,
        archive_stores={"archive": primary, "secondary": secondary},
        archive_read_order=("secondary", "archive"),
    )
    monkeypatch.setattr(deps, "StorageAdapterArchiveStore", _ArchiveAdapter)
    monkeypatch.setattr(deps, "StorageAdapterObjectStore", _ObjectAdapter)
    runtime = object()

    registry = deps._archive_store_registry(
        config,
        runtimes={"archive": runtime},  # type: ignore[dict-item]
        retrieval_cache=None,
        download_allowance=object(),  # type: ignore[arg-type]
    )

    assert registry.names == tuple(config.archive_stores)
    for name in registry.names:
        binding = registry.require(name)
        expected = config.archive_store(name)
        assert binding.store.store_config is expected  # type: ignore[attr-defined]
        assert binding.store.runtime is runtime  # type: ignore[attr-defined]
        assert binding.multipart_objects is binding.immutable_objects
        assert binding.multipart_objects is binding.object_ranges
        assert binding.multipart_objects.runtime is runtime  # type: ignore[attr-defined]
