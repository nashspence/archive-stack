MAX_LIST_PAGE_SIZE = 100

ATTEMPT_LIST_SORT_FIELDS = frozenset(
    {
        "attempt_number",
        "bytes",
        "created_at",
        "file_count",
        "run_id",
        "target_submission_id",
        "state",
        "target",
        "updated_at",
    }
)

SOURCE_LIST_SORT_FIELDS = frozenset(
    {
        "cadence",
        "created_at",
        "enabled",
        "id",
        "target",
        "updated_at",
    }
)
