from __future__ import annotations

from collections.abc import Mapping

from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt


def retrieval_cache_receipt_payload(
    receipt: RetrievalCacheReceipt | None,
) -> dict[str, object] | None:
    if receipt is None:
        return None
    return {
        "cache_store": receipt.cache_store,
        "object_path": receipt.object_path,
        "revision": receipt.revision,
        "stored_bytes": receipt.stored_bytes,
        "stored_sha256": receipt.stored_sha256,
        "cached_at": receipt.cached_at,
        "verified_at": receipt.verified_at,
    }


def parse_retrieval_cache_receipt(value: object) -> RetrievalCacheReceipt | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("retrieval cache receipt is invalid")
    object_path = str(value.get("object_path", ""))
    cache_store = str(value.get("cache_store", ""))
    stored_sha256_value = value.get("stored_sha256")
    stored_sha256 = str(stored_sha256_value) if stored_sha256_value is not None else None
    cached_at = str(value.get("cached_at", ""))
    verified_at = str(value.get("verified_at", ""))
    stored_bytes = value.get("stored_bytes")
    if (
        not cache_store
        or not object_path
        or isinstance(stored_bytes, bool)
        or not isinstance(stored_bytes, int)
        or stored_bytes < 1
        or (
            stored_sha256 is not None
            and (
                len(stored_sha256) != 64
                or any(character not in "0123456789abcdef" for character in stored_sha256)
            )
        )
        or not cached_at
        or not verified_at
    ):
        raise ValueError("retrieval cache receipt fields are invalid")
    revision = value.get("revision")
    return RetrievalCacheReceipt(
        cache_store=cache_store,
        object_path=object_path,
        revision=str(revision) if revision is not None else None,
        stored_bytes=stored_bytes,
        stored_sha256=stored_sha256,
        cached_at=cached_at,
        verified_at=verified_at,
    )
