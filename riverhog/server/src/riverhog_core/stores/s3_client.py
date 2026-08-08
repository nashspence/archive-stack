from __future__ import annotations

from typing import Any

from riverhog_core.runtime_config import ArchiveStoreConfig, RuntimeConfig
from riverhog_core.throughput import S3TransportTuning


def create_archive_s3_client(
    config: RuntimeConfig,
    store: ArchiveStoreConfig,
    *,
    tuning: S3TransportTuning | None = None,
) -> Any:
    """Create an archive client whose socket pool cannot silently defeat concurrency knobs."""

    try:
        import boto3
        from botocore.config import Config
    except Exception as exc:  # pragma: no cover - runtime dependency guard
        raise RuntimeError("S3-backed archive support requires boto3/botocore") from exc
    effective = tuning or S3TransportTuning(max_pool_connections=config.s3_max_pool_connections)
    return boto3.client(
        "s3",
        endpoint_url=store.endpoint_url,
        region_name=store.region,
        aws_access_key_id=store.access_key_id,
        aws_secret_access_key=store.secret_access_key,
        config=Config(
            max_pool_connections=effective.max_pool_connections,
            connect_timeout=effective.connect_timeout_seconds,
            read_timeout=effective.read_timeout_seconds,
            tcp_keepalive=effective.tcp_keepalive,
            retries={
                "mode": effective.retry_mode,
                "max_attempts": effective.max_attempts,
            },
            s3={"addressing_style": "path" if store.force_path_style else "virtual"},
        ),
    )
