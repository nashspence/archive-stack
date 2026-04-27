from __future__ import annotations

from typing import Any

from arc_core.runtime_config import RuntimeConfig


def _require_boto3() -> tuple[Any, Any]:
    try:
        import boto3
        from botocore.config import Config
    except Exception as exc:  # pragma: no cover - runtime dependency guard
        raise RuntimeError(
            "S3-backed runtime support requires boto3/botocore to be installed"
        ) from exc
    return boto3, Config


def create_s3_client(config: RuntimeConfig) -> Any:
    boto3, Config = _require_boto3()
    return boto3.client(
        "s3",
        endpoint_url=config.s3_endpoint_url,
        region_name=config.s3_region,
        aws_access_key_id=config.s3_access_key_id,
        aws_secret_access_key=config.s3_secret_access_key,
        config=Config(
            s3={"addressing_style": "path" if config.s3_force_path_style else "virtual"}
        ),
    )


def ensure_bucket_exists(config: RuntimeConfig) -> None:
    client = create_s3_client(config)
    existing = client.list_buckets().get("Buckets", [])
    if any(bucket.get("Name") == config.s3_bucket for bucket in existing):
        return
    create_kwargs: dict[str, object] = {"Bucket": config.s3_bucket}
    if config.s3_region and config.s3_region != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": config.s3_region}
    client.create_bucket(**create_kwargs)


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
