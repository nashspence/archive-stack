from __future__ import annotations

import json

from arc_core.runtime_config import load_runtime_config
from arc_core.stores.s3_support import create_glacier_s3_client, create_s3_client

EXPECTED_LIFECYCLE_CONFIGURATION = {
    "Rules": [
        {
            "ID": "abort-incomplete-riverhog-uploads",
            "Status": "Enabled",
            "Filter": {},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 3},
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


def _lifecycle_targets(config) -> list[tuple[object, str]]:
    targets: list[tuple[object, str]] = [(create_s3_client(config), config.s3_bucket)]
    archive_signature = (
        config.glacier_endpoint_url,
        config.glacier_region,
        config.glacier_bucket,
        config.glacier_access_key_id,
        config.glacier_force_path_style,
    )
    storage_signature = (
        config.s3_endpoint_url,
        config.s3_region,
        config.s3_bucket,
        config.s3_access_key_id,
        config.s3_force_path_style,
    )
    if archive_signature != storage_signature:
        targets.append((create_glacier_s3_client(config), config.glacier_bucket))
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
    config = load_runtime_config()
    for client, bucket in _lifecycle_targets(config):
        _configure_bucket_lifecycle(client=client, bucket=bucket)


if __name__ == "__main__":
    main()
