from __future__ import annotations

from typing import Any

from riverhog_core.runtime_config import RuntimeConfig

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


def create_s3_client(config: RuntimeConfig) -> Any:
    return _create_s3_client(
        endpoint_url=config.s3_endpoint_url,
        region=config.s3_region,
        access_key_id=config.s3_access_key_id,
        secret_access_key=config.s3_secret_access_key,
        force_path_style=config.s3_force_path_style,
        max_pool_connections=config.s3_max_pool_connections,
    )


def create_archive_s3_client(config: RuntimeConfig) -> Any:
    return _create_s3_client(
        endpoint_url=config.archive_endpoint_url,
        region=config.archive_region,
        access_key_id=config.archive_access_key_id,
        secret_access_key=config.archive_secret_access_key,
        force_path_style=config.archive_force_path_style,
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
        create_s3_client(config),
        bucket=config.s3_bucket,
        region=config.s3_region,
    )
    if (
        config.archive_bucket == config.s3_bucket
        and config.archive_endpoint_url == config.s3_endpoint_url
    ):
        return
    _ensure_bucket_exists(
        create_archive_s3_client(config),
        bucket=config.archive_bucket,
        region=config.archive_region,
    )


def delete_keys_with_prefixes(config: RuntimeConfig, prefixes: list[str]) -> None:
    client = create_s3_client(config)
    for prefix in prefixes:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=config.s3_bucket, Prefix=prefix):
            contents = page.get("Contents", [])
            if not contents:
                continue
            client.delete_objects(
                Bucket=config.s3_bucket,
                Delete={"Objects": [{"Key": entry["Key"]} for entry in contents]},
            )

    if (
        config.archive_bucket == config.s3_bucket
        and config.archive_endpoint_url == config.s3_endpoint_url
    ):
        return

    archive_client = create_archive_s3_client(config)
    for prefix in prefixes:
        paginator = archive_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=config.archive_bucket, Prefix=prefix):
            contents = page.get("Contents", [])
            if not contents:
                continue
            archive_client.delete_objects(
                Bucket=config.archive_bucket,
                Delete={"Objects": [{"Key": entry["Key"]} for entry in contents]},
            )
