MAX_LIST_PAGE_SIZE = 100

ATTEMPT_LIST_SORT_FIELDS = frozenset(
    {
        "attempt_number",
        "bytes",
        "collection_slug",
        "collection_timestamp",
        "created_at",
        "file_count",
        "target_submission_id",
        "state",
        "target",
        "updated_at",
    }
)

SOURCE_LIST_SORT_FIELDS = frozenset(
    {
        "cadence",
        "collection_slug",
        "created_at",
        "enabled",
        "id",
        "template",
        "target",
        "updated_at",
    }
)
