from __future__ import annotations

from collections.abc import Mapping

from riverhog_core.ports.retrieval_cache import RetrievalCacheReceipt


def retrieval_cache_receipt_payload(
    receipt: RetrievalCacheReceipt | None,
) -> dict[str, object] | None:
    if receipt is None:
        return None
    return {
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
    stored_sha256 = str(value.get("stored_sha256", ""))
    cached_at = str(value.get("cached_at", ""))
    verified_at = str(value.get("verified_at", ""))
    stored_bytes = value.get("stored_bytes")
    if (
        not object_path
        or isinstance(stored_bytes, bool)
        or not isinstance(stored_bytes, int)
        or stored_bytes < 1
        or len(stored_sha256) != 64
        or any(character not in "0123456789abcdef" for character in stored_sha256)
        or not cached_at
        or not verified_at
    ):
        raise ValueError("retrieval cache receipt fields are invalid")
    revision = str(value.get("revision", ""))
    if not revision:
        raise ValueError("retrieval cache receipt revision is invalid")
    return RetrievalCacheReceipt(
        object_path=object_path,
        revision=revision,
        stored_bytes=stored_bytes,
        stored_sha256=stored_sha256,
        cached_at=cached_at,
        verified_at=verified_at,
    )
