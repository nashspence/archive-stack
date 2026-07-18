from __future__ import annotations

import json

from riverhog_core.runtime_config import load_runtime_config
from riverhog_core.stores.s3_support import (
    create_archive_s3_client,
    create_ingress_s3_client,
    create_retrieval_cache_s3_client,
)

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
    ingress = config.ingress_store
    targets: list[tuple[object, str]] = [
        (create_ingress_s3_client(config, ingress), ingress.bucket)
    ]
    storage_signature = (
        ingress.endpoint_url,
        ingress.region,
        ingress.bucket,
        ingress.access_key_id,
        ingress.force_path_style,
    )
    seen = {storage_signature}
    for store in config.archive_stores.values():
        archive_signature = (
            store.endpoint_url,
            store.region,
            store.bucket,
            store.access_key_id,
            store.force_path_style,
        )
        if archive_signature in seen:
            continue
        seen.add(archive_signature)
        targets.append((create_archive_s3_client(config, store), store.bucket))
    if config.retrieval_cache is not None:
        cache = config.retrieval_cache
        cache_signature = (
            cache.endpoint_url,
            cache.region,
            cache.bucket,
            cache.access_key_id,
            cache.force_path_style,
        )
        if cache_signature not in seen:
            targets.append((create_retrieval_cache_s3_client(config, cache), cache.bucket))
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
