from __future__ import annotations

from dataclasses import replace

import riverhog_api.deps as deps
from riverhog_core.runtime_config import RuntimeConfig


class _Adapter:
    def __init__(self, _config: RuntimeConfig, store: object, **_kwargs: object) -> None:
        self.store_config = store


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
    monkeypatch.setattr(deps, "S3ArchiveStore", _Adapter)
    monkeypatch.setattr(deps, "S3ArchiveMultipartObjectStore", _Adapter)
    monkeypatch.setattr(deps, "S3ImmutableArchiveObjectStore", _Adapter)
    monkeypatch.setattr(deps, "S3ArchiveObjectRangeStore", _Adapter)

    registry = deps._archive_store_registry(
        config,
        retrieval_cache=None,
        download_allowance=object(),  # type: ignore[arg-type]
    )

    assert registry.names == tuple(config.archive_stores)
    for name in registry.names:
        binding = registry.require(name)
        expected = config.archive_store(name)
        assert binding.store.store_config is expected  # type: ignore[attr-defined]
        assert binding.multipart_objects.store_config is expected  # type: ignore[attr-defined]
        assert binding.immutable_objects.store_config is expected  # type: ignore[attr-defined]
        assert binding.object_ranges.store_config is expected  # type: ignore[attr-defined]
