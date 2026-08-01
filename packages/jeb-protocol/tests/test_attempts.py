from jeb_protocol import (
    ATTEMPT_RESOLVED_STATES,
    attempt_succeeded,
    attempt_watch_finished,
)


def test_jeb_attempt_resolution_and_watch_outcomes_are_distinct() -> None:
    assert ATTEMPT_RESOLVED_STATES == {
        "target_succeeded",
        "cleanup_done",
        "superseded",
        "canceled",
    }
    assert attempt_succeeded({"state": "target_succeeded"})
    assert attempt_succeeded({"state": "cleanup_done"})
    assert not attempt_succeeded({"state": "superseded"})
    assert attempt_watch_finished({"state": "failed"})
    assert attempt_watch_finished({"state": "cleanup_failed"})
    assert attempt_watch_finished({"state": "superseded"})
    assert attempt_watch_finished({"state": "canceled"})
    assert not attempt_watch_finished({"state": "target_uploading"})
