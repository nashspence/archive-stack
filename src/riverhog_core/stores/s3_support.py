from __future__ import annotations

from typing import Any

from riverhog_core.runtime_config import ArchiveStoreConfig, RuntimeConfig

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
        config=Config(
            max_pool_connections=max_pool_connections,
            s3={"addressing_style": "path" if force_path_style else "virtual"},
        ),
    )


def create_hot_store_client(config: RuntimeConfig) -> Any:
    return _create_s3_client(
        endpoint_url=config.hot_store_endpoint_url,
        region=config.hot_store_region,
        access_key_id=config.hot_store_access_key_id,
        secret_access_key=config.hot_store_secret_access_key,
        force_path_style=config.hot_store_force_path_style,
        max_pool_connections=config.s3_max_pool_connections,
    )


def create_archive_s3_client(config: RuntimeConfig, store: ArchiveStoreConfig) -> Any:
    return _create_s3_client(
        endpoint_url=store.endpoint_url,
        region=store.region,
        access_key_id=store.access_key_id,
        secret_access_key=store.secret_access_key,
        force_path_style=store.force_path_style,
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
    _ensure_bucket_exists(
        create_hot_store_client(config),
        bucket=config.hot_store_bucket,
        region=config.hot_store_region,
    )
    seen = {(config.hot_store_endpoint_url, config.hot_store_bucket)}
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


def delete_keys_with_prefixes(config: RuntimeConfig, prefixes: list[str]) -> None:
    client = create_hot_store_client(config)
    for prefix in prefixes:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=config.hot_store_bucket, Prefix=prefix):
            contents = page.get("Contents", [])
            if not contents:
                continue
            client.delete_objects(
                Bucket=config.hot_store_bucket,
                Delete={"Objects": [{"Key": entry["Key"]} for entry in contents]},
            )

    seen = {(config.hot_store_endpoint_url, config.hot_store_bucket)}
    for store in config.archive_stores.values():
        signature = (store.endpoint_url, store.bucket)
        if signature in seen:
            continue
        archive_client = create_archive_s3_client(config, store)
        for prefix in prefixes:
            paginator = archive_client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=store.bucket, Prefix=prefix):
                contents = page.get("Contents", [])
                if not contents:
                    continue
                archive_client.delete_objects(
                    Bucket=store.bucket,
                    Delete={"Objects": [{"Key": entry["Key"]} for entry in contents]},
                )
        seen.add(signature)
