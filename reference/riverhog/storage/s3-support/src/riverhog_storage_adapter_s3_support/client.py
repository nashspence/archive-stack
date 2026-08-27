"""S3 client construction owned by an adapter process, never Riverhog core."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class S3TransportTuning:
    max_pool_connections: int = 32
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 300.0
    max_attempts: int = 8
    retry_mode: Literal["standard", "adaptive"] = "standard"
    tcp_keepalive: bool = True

    def __post_init__(self) -> None:
        if self.max_pool_connections < 1 or self.max_pool_connections > 4096:
            raise ValueError("S3 maximum pool connections must be within 1..4096")
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("S3 timeouts must be positive")
        if self.max_attempts < 1 or self.max_attempts > 100:
            raise ValueError("S3 maximum attempts must be within 1..100")


@dataclass(frozen=True, slots=True)
class S3ClientConfig:
    endpoint_url: str | None
    region: str
    access_key_id: str
    secret_access_key: str
    session_token: str | None = None
    force_path_style: bool = False

    def __post_init__(self) -> None:
        if not self.region.strip():
            raise ValueError("S3 region must be nonempty")
        if not self.access_key_id or not self.secret_access_key:
            raise ValueError("S3 credentials must be nonempty")


def create_s3_client(
    config: S3ClientConfig,
    *,
    tuning: S3TransportTuning | None = None,
) -> Any:
    """Create one pooled S3 client using adapter-owned transport policy."""

    try:
        import boto3
        from botocore.config import Config
    except Exception as exc:  # pragma: no cover - runtime dependency guard
        raise RuntimeError("S3 adapter support requires boto3/botocore") from exc
    effective = tuning or S3TransportTuning()
    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        region_name=config.region,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        aws_session_token=config.session_token,
        config=Config(
            max_pool_connections=effective.max_pool_connections,
            connect_timeout=effective.connect_timeout_seconds,
            read_timeout=effective.read_timeout_seconds,
            tcp_keepalive=effective.tcp_keepalive,
            # Riverhog observes the declared byte count and digest while the
            # provider consumes the one-pass body.  Botocore's optional
            # checksum mode pre-reads and rewinds file-like bodies, which is
            # incompatible with that custody boundary.  Required provider
            # checksums remain enabled.
            request_checksum_calculation="when_required",
            retries={
                "mode": effective.retry_mode,
                "max_attempts": effective.max_attempts,
            },
            s3={
                "addressing_style": "path" if config.force_path_style else "virtual",
                # A signed payload hash would likewise require a complete
                # pre-read.  Provider transport security and Riverhog's inline
                # custody verification protect the one-pass body instead.
                "payload_signing_enabled": False,
            },
        ),
    )


__all__ = ["S3ClientConfig", "S3TransportTuning", "create_s3_client"]
