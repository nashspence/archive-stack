from __future__ import annotations

import sys

import pytest
from riverhog_core.runtime_config import ArchiveStoreConfig, RetrievalCacheConfig, RuntimeConfig
from riverhog_core.stores import s3_client as tuned_s3_client
from riverhog_core.stores import s3_support
from riverhog_core.stores.s3_support import (
    _delete_object_versions,
    _ensure_bucket_exists,
    delete_exact_object,
)


class _FakeS3Error(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class _UnsupportedVersionListing(Exception):
    def __init__(self) -> None:
        super().__init__("version listing is unavailable")
        self.response = {
            "Error": {"Code": "NotImplemented"},
            "ResponseMetadata": {"HTTPStatusCode": 501},
        }


class _Boto3:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def client(self, service: str, **kwargs: object) -> object:
        assert service == "s3"
        self.requests.append(kwargs)
        return object()


class _Config:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


def test_cache_client_forwards_temporary_session_token(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    boto3 = _Boto3()
    monkeypatch.setattr(s3_support, "_require_boto3", lambda: (boto3, _Config))
    archive = ArchiveStoreConfig(
        name="deep",
        endpoint_url="https://s3.us-west-2.amazonaws.com",
        region="us-west-2",
        bucket="archive",
        access_key_id="archive-key",
        secret_access_key="archive-secret",
        session_token="archive-session",
        force_path_style=False,
        prefix="qualification",
        backend="aws",
        storage_class="DEEP_ARCHIVE",
    )
    cache = RetrievalCacheConfig(
        endpoint_url="https://s3.us-west-004.backblazeb2.com",
        region="us-west-004",
        bucket="cache",
        access_key_id="cache-key",
        secret_access_key="cache-secret",
        session_token="cache-session",
    )
    config = RuntimeConfig(
        archive_write_store="deep",
        archive_read_order=("deep",),
        archive_stores={"deep": archive},
        retrieval_cache=cache,
        archive_passphrase="qualification-passphrase",
    )

    s3_support.create_retrieval_cache_s3_client(config, cache)

    assert boto3.requests[0]["aws_session_token"] == "cache-session"


def test_tuned_archive_client_forwards_temporary_session_token(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    boto3 = _Boto3()

    class _BotocoreConfig:
        Config = _Config

    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore.config", _BotocoreConfig())
    archive = ArchiveStoreConfig(
        name="deep",
        endpoint_url="https://s3.us-west-2.amazonaws.com",
        region="us-west-2",
        bucket="archive",
        access_key_id="archive-key",
        secret_access_key="archive-secret",
        session_token="archive-session",
        force_path_style=False,
        prefix="qualification",
        backend="aws",
        storage_class="DEEP_ARCHIVE",
    )
    config = RuntimeConfig(
        archive_write_store="deep",
        archive_read_order=("deep",),
        archive_stores={"deep": archive},
        archive_passphrase="qualification-passphrase",
    )

    tuned_s3_client.create_archive_s3_client(config, archive)

    assert boto3.requests[0]["aws_session_token"] == "archive-session"
    assert s3_support.create_archive_s3_client is tuned_s3_client.create_archive_s3_client


def test_ensure_bucket_exists_uses_head_bucket_for_named_bucket() -> None:
    class Client:
        def __init__(self) -> None:
            self.head_buckets: list[str] = []

        def head_bucket(self, *, Bucket: str) -> None:
            self.head_buckets.append(Bucket)

        def list_buckets(self) -> None:
            raise AssertionError("bucket checks must not require ListBuckets permission")

        def create_bucket(self, **_kwargs: object) -> None:
            raise AssertionError("existing bucket should not be created")

    client = Client()

    _ensure_bucket_exists(client, bucket="archive", region="us-west-2")

    assert client.head_buckets == ["archive"]


def test_ensure_bucket_exists_creates_missing_bucket() -> None:
    class Client:
        def __init__(self) -> None:
            self.create_kwargs: dict[str, object] | None = None

        def head_bucket(self, *, Bucket: str) -> None:
            assert Bucket == "archive"
            raise _FakeS3Error("404", 404)

        def list_buckets(self) -> None:
            raise AssertionError("bucket checks must not require ListBuckets permission")

        def create_bucket(self, **kwargs: object) -> None:
            self.create_kwargs = kwargs

    client = Client()

    _ensure_bucket_exists(client, bucket="archive", region="us-west-2")

    assert client.create_kwargs == {
        "Bucket": "archive",
        "CreateBucketConfiguration": {"LocationConstraint": "us-west-2"},
    }


def test_ensure_bucket_exists_reraises_non_missing_bucket_error() -> None:
    class Client:
        def head_bucket(self, *, Bucket: str) -> None:
            assert Bucket == "archive"
            raise _FakeS3Error("AccessDenied", 403)

        def create_bucket(self, **_kwargs: object) -> None:
            raise AssertionError("permission failures should not create buckets")

    with pytest.raises(_FakeS3Error):
        _ensure_bucket_exists(Client(), bucket="archive", region="us-west-2")


def test_delete_exact_object_removes_current_and_exact_versions_only() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Paginator:
        def paginate(self, **_kwargs: object):
            return [
                {
                    "Versions": [
                        {"Key": "object", "VersionId": "v1"},
                        {"Key": "object-neighbor", "VersionId": "v2"},
                    ],
                    "DeleteMarkers": [{"Key": "object", "VersionId": "marker"}],
                }
            ]

    class Client:
        def delete_object(self, **kwargs: object) -> None:
            calls.append(("current", kwargs))

        def get_paginator(self, name: str) -> Paginator:
            assert name == "list_object_versions"
            return Paginator()

        def delete_objects(self, **kwargs: object) -> None:
            calls.append(("versions", kwargs))

    delete_exact_object(Client(), bucket="archive", key="object")

    assert calls == [
        ("current", {"Bucket": "archive", "Key": "object"}),
        (
            "versions",
            {
                "Bucket": "archive",
                "Delete": {
                    "Objects": [
                        {"Key": "object", "VersionId": "v1"},
                        {"Key": "object", "VersionId": "marker"},
                    ]
                },
            },
        ),
    ]


def test_delete_prefix_removes_current_objects_versions_and_markers() -> None:
    calls: list[dict[str, object]] = []

    class Paginator:
        def __init__(self, name: str) -> None:
            self.name = name

        def paginate(self, **_kwargs: object):
            if self.name == "list_objects_v2":
                return [{"Contents": [{"Key": "prefix/current"}]}]
            return [
                {
                    "Versions": [{"Key": "prefix/object", "VersionId": "v1"}],
                    "DeleteMarkers": [{"Key": "prefix/object", "VersionId": "marker"}],
                }
            ]

    class Client:
        def get_paginator(self, name: str) -> Paginator:
            return Paginator(name)

        def delete_objects(self, **kwargs: object) -> None:
            calls.append(kwargs)

    _delete_object_versions(Client(), bucket="archive", prefixes=["prefix/"])

    assert calls == [
        {"Bucket": "archive", "Delete": {"Objects": [{"Key": "prefix/current"}]}},
        {
            "Bucket": "archive",
            "Delete": {
                "Objects": [
                    {"Key": "prefix/object", "VersionId": "v1"},
                    {"Key": "prefix/object", "VersionId": "marker"},
                ]
            },
        },
    ]


def test_delete_exact_object_accepts_explicitly_unversioned_provider() -> None:
    deleted: list[dict[str, object]] = []

    class Paginator:
        def paginate(self, **_kwargs: object):
            raise _UnsupportedVersionListing

    class Client:
        def delete_object(self, **kwargs: object) -> None:
            deleted.append(kwargs)

        def get_paginator(self, name: str) -> Paginator:
            assert name == "list_object_versions"
            return Paginator()

    delete_exact_object(Client(), bucket="archive", key="object")

    assert deleted == [{"Bucket": "archive", "Key": "object"}]
