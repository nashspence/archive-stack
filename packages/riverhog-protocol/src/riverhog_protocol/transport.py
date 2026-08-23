"""Bounded API transport batches that do not limit domain cardinality."""

COLLECTION_UPLOAD_FILE_BATCH_MAX = 100
RETRIEVAL_FILE_BATCH_MAX = 10_000

__all__ = [
    "COLLECTION_UPLOAD_FILE_BATCH_MAX",
    "RETRIEVAL_FILE_BATCH_MAX",
]
