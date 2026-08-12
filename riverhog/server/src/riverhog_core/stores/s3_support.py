from __future__ import annotations

from typing import Any

from riverhog_core.runtime_config import (
    RetrievalCacheConfig,
    RuntimeConfig,
)
from riverhog_core.stores.s3_client import create_archive_s3_client as create_archive_s3_client

_MISSING_BUCKET_CODES = {"404", "NoSuchBucket", "NotFound"}


def _require_boto3() -> tuple[Any, Any]:
    try:
        import boto3
        from botocore.config import Config
    except Exception as exc:  # pragma: no cover - runtime dependency guard
        raise RuntimeError(
            "S3-backed runtime support requires boto3/botocore to be installed"
        ) from exc
    return boto3, Config


def _create_s3_client(
    *,
    endpoint_url: str,
    region: str,
    access_key_id: str,
    secret_access_key: str,
    session_token: str | None,
    force_path_style: bool,
    max_pool_connections: int,
) -> Any:
    boto3, Config = _require_boto3()
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        aws_session_token=session_token,
        config=Config(
            max_pool_connections=max_pool_connections,
            s3={"addressing_style": "path" if force_path_style else "virtual"},
        ),
    )


def create_retrieval_cache_s3_client(
    config: RuntimeConfig,
    cache: RetrievalCacheConfig,
) -> Any:
    return _create_s3_client(
        endpoint_url=cache.endpoint_url,
        region=cache.region,
        access_key_id=cache.access_key_id,
        secret_access_key=cache.secret_access_key,
        session_token=cache.session_token,
        force_path_style=cache.force_path_style,
        max_pool_connections=config.s3_max_pool_connections,
    )


def _bucket_missing(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    if isinstance(error, dict) and str(error.get("Code", "")) in _MISSING_BUCKET_CODES:
        return True
    metadata = response.get("ResponseMetadata", {})
    return isinstance(metadata, dict) and metadata.get("HTTPStatusCode") == 404


def _object_version_listing_unsupported(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    if isinstance(error, dict) and str(error.get("Code", "")) in {
        "NotImplemented",
        "UnsupportedOperation",
    }:
        return True
    metadata = response.get("ResponseMetadata", {})
    return isinstance(metadata, dict) and metadata.get("HTTPStatusCode") == 501


def _ensure_bucket_exists(client: Any, *, bucket: str, region: str) -> None:
    try:
        client.head_bucket(Bucket=bucket)
        return
    except Exception as exc:
        if not _bucket_missing(exc):
            raise

    create_kwargs: dict[str, object] = {"Bucket": bucket}
    if region and region != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    client.create_bucket(**create_kwargs)


def ensure_bucket_exists(config: RuntimeConfig) -> None:
    seen: set[tuple[str, str]] = set()
    for store in config.archive_stores.values():
        signature = (store.endpoint_url, store.bucket)
        if signature in seen:
            continue
        _ensure_bucket_exists(
            create_archive_s3_client(config, store),
            bucket=store.bucket,
            region=store.region,
        )
        seen.add(signature)
    if config.retrieval_cache is not None:
        cache = config.retrieval_cache
        signature = (cache.endpoint_url, cache.bucket)
        if signature not in seen:
            _ensure_bucket_exists(
                create_retrieval_cache_s3_client(config, cache),
                bucket=cache.bucket,
                region=cache.region,
            )


def delete_keys_with_prefixes(config: RuntimeConfig, prefixes: list[str]) -> None:
    seen: set[tuple[str, str]] = set()
    for store in config.archive_stores.values():
        signature = (store.endpoint_url, store.bucket)
        if signature in seen:
            continue
        archive_client = create_archive_s3_client(config, store)
        _delete_object_versions(archive_client, bucket=store.bucket, prefixes=prefixes)
        seen.add(signature)
    if config.retrieval_cache is not None:
        cache = config.retrieval_cache
        cache_client = create_retrieval_cache_s3_client(config, cache)
        _delete_object_versions(cache_client, bucket=cache.bucket, prefixes=prefixes)


def delete_object_versions_with_prefix(client: Any, *, bucket: str, prefix: str) -> None:
    _delete_object_versions(client, bucket=bucket, prefixes=[prefix])


def _delete_object_versions(client: Any, *, bucket: str, prefixes: list[str]) -> None:
    for prefix in prefixes:
        current = client.get_paginator("list_objects_v2")
        for page in current.paginate(Bucket=bucket, Prefix=prefix):
            objects = [{"Key": entry["Key"]} for entry in page.get("Contents", [])]
            if objects:
                client.delete_objects(Bucket=bucket, Delete={"Objects": objects})
        try:
            paginator = client.get_paginator("list_object_versions")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                versions = [
                    {"Key": entry["Key"], "VersionId": entry["VersionId"]}
                    for entry in [
                        *(page.get("Versions") or ()),
                        *(page.get("DeleteMarkers") or ()),
                    ]
                ]
                if versions:
                    client.delete_objects(Bucket=bucket, Delete={"Objects": versions})
        except Exception as exc:
            if not _object_version_listing_unsupported(exc):
                raise


def delete_exact_object(client: Any, *, bucket: str, key: str) -> None:
    client.delete_object(Bucket=bucket, Key=key)
    try:
        paginator = client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket, Prefix=key):
            versions = [
                {"Key": entry["Key"], "VersionId": entry["VersionId"]}
                for entry in [
                    *(page.get("Versions") or ()),
                    *(page.get("DeleteMarkers") or ()),
                ]
                if entry.get("Key") == key
            ]
            if versions:
                client.delete_objects(Bucket=bucket, Delete={"Objects": versions})
    except Exception as exc:
        if not _object_version_listing_unsupported(exc):
            raise
