from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from riverhog_core.runtime_config import (
    IngressStoreConfig,
    RetrievalCacheConfig,
    RuntimeConfig,
)

from tests.harness import configure_garage
from tests.unit.db_helpers import sqlite_url


class _FakeS3Client:
    def __init__(self) -> None:
        self.put_buckets: list[str] = []
        self.lifecycle_by_bucket: dict[str, dict[str, object]] = {}

    def put_bucket_lifecycle_configuration(
        self,
        *,
        Bucket: str,
        LifecycleConfiguration: dict[str, object],
    ) -> None:
        self.put_buckets.append(Bucket)
        self.lifecycle_by_bucket[Bucket] = LifecycleConfiguration

    def get_bucket_lifecycle_configuration(self, *, Bucket: str) -> dict[str, object]:
        return self.lifecycle_by_bucket[Bucket]


def _config(tmp_path: Path, **overrides: object) -> RuntimeConfig:
    config = RuntimeConfig(
        ingress_store=IngressStoreConfig(
            endpoint_url="http://garage:3900",
            region="garage",
            bucket="riverhog-ingress",
            access_key_id="GK000000000000000000000002",
            secret_access_key="2" * 64,
            force_path_style=True,
        ),
        retrieval_cache=RetrievalCacheConfig(
            endpoint_url="http://garage:3900",
            region="garage",
            bucket="riverhog-retrieval-cache",
            access_key_id="GK000000000000000000000002",
            secret_access_key="2" * 64,
            force_path_style=True,
        ),
        tusd_base_url="http://example.invalid:1080/files",
        tusd_hook_secret="hook-secret",
        database_url=sqlite_url(tmp_path / "state.sqlite3"),
    )
    archive_bucket = overrides.pop("archive_bucket", None)
    config = replace(config, **overrides)
    if archive_bucket is None:
        return config
    store = replace(config.archive_store("archive"), bucket=str(archive_bucket))
    return replace(config, archive_stores={"archive": store})


def test_lifecycle_targets_cover_each_distinct_object_store_bucket(tmp_path: Path) -> None:
    config = _config(tmp_path, archive_bucket="riverhog-archive")
    ingress_client = _FakeS3Client()
    archive_client = _FakeS3Client()
    cache_client = _FakeS3Client()

    original_ingress = configure_garage.create_ingress_s3_client
    original_archive = configure_garage.create_archive_s3_client
    original_cache = configure_garage.create_retrieval_cache_s3_client
    configure_garage.create_ingress_s3_client = lambda *args: ingress_client  # type: ignore[assignment]
    configure_garage.create_archive_s3_client = (  # type: ignore[assignment]
        lambda current, store: archive_client
    )
    configure_garage.create_retrieval_cache_s3_client = lambda *args: cache_client  # type: ignore[assignment]
    try:
        targets = configure_garage._lifecycle_targets(config)
    finally:
        configure_garage.create_ingress_s3_client = original_ingress  # type: ignore[assignment]
        configure_garage.create_archive_s3_client = original_archive  # type: ignore[assignment]
        configure_garage.create_retrieval_cache_s3_client = original_cache  # type: ignore[assignment]

    assert targets == [
        (ingress_client, "riverhog-ingress"),
        (archive_client, "riverhog-archive"),
        (cache_client, "riverhog-retrieval-cache"),
    ]


def test_configure_bucket_lifecycle_verifies_expected_payload() -> None:
    client = _FakeS3Client()

    configure_garage._configure_bucket_lifecycle(client=client, bucket="riverhog-archive")

    assert client.put_buckets == ["riverhog-archive"]
    assert client.lifecycle_by_bucket["riverhog-archive"] == (
        configure_garage.EXPECTED_LIFECYCLE_CONFIGURATION
    )
