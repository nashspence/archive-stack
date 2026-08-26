from __future__ import annotations

import json
import os
from collections.abc import Mapping

from riverhog_storage_adapter_s3_support import (
    S3ClientConfig,
    create_s3_client,
)

EXPECTED_LIFECYCLE_CONFIGURATION = {
    "Rules": [
        {
            "ID": "abort-incomplete-riverhog-uploads",
            "Status": "Enabled",
            "Filter": {},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 4},
        }
    ]
}


def _normalize_lifecycle_configuration(payload: dict[str, object]) -> dict[str, object]:
    rules = []
    for rule in payload.get("Rules", []):
        if not isinstance(rule, dict):
            continue
        rules.append(
            {
                "ID": rule.get("ID"),
                "Status": rule.get("Status"),
                "Filter": rule.get("Filter", {}),
                "AbortIncompleteMultipartUpload": {
                    "DaysAfterInitiation": rule.get("AbortIncompleteMultipartUpload", {}).get(
                        "DaysAfterInitiation"
                    )
                },
            }
        )
    return {"Rules": rules}


def _lifecycle_targets(values: Mapping[str, str]) -> list[tuple[object, str]]:
    targets: list[tuple[object, str]] = []
    seen: set[tuple[str, ...]] = set()
    endpoint_url = values.get("RIVERHOG_GARAGE_STORAGE_ADAPTER_ENDPOINT_URL", "http://garage:3900")
    region = values.get("RIVERHOG_GARAGE_STORAGE_ADAPTER_REGION", "garage")
    for role, default_bucket in (("ARCHIVE", "riverhog-archive"), ("CACHE", "riverhog-cache")):
        bucket = values.get(f"RIVERHOG_GARAGE_{role}_BUCKET", default_bucket)
        access_key_id = values.get(
            f"RIVERHOG_GARAGE_{role}_ACCESS_KEY_ID", "GK000000000000000000000002"
        )
        secret_access_key = values.get(f"RIVERHOG_GARAGE_{role}_SECRET_ACCESS_KEY", "2" * 64)
        signature = (endpoint_url, region, bucket, access_key_id)
        if signature in seen:
            continue
        seen.add(signature)
        client = create_s3_client(
            S3ClientConfig(
                endpoint_url=endpoint_url,
                region=region,
                access_key_id=access_key_id,
                secret_access_key=secret_access_key,
                force_path_style=True,
            )
        )
        targets.append((client, bucket))
    return targets


def _configure_bucket_lifecycle(*, client, bucket: str) -> None:
    client.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration=EXPECTED_LIFECYCLE_CONFIGURATION,
    )
    actual = client.get_bucket_lifecycle_configuration(Bucket=bucket)
    normalized = _normalize_lifecycle_configuration(actual)
    if normalized != EXPECTED_LIFECYCLE_CONFIGURATION:
        raise SystemExit(
            f"unexpected lifecycle configuration for bucket {bucket}:\n"
            + json.dumps(normalized, indent=2, sort_keys=True)
        )


def main() -> None:
    for client, bucket in _lifecycle_targets(os.environ):
        _configure_bucket_lifecycle(client=client, bucket=bucket)


if __name__ == "__main__":
    main()
