from riverhog_cli_support.output import (
    format_lifecycle_events,
    format_list_ids,
    human_bytes,
    json_text,
    mapping_items,
    page_line,
)


def test_shared_cli_output_projects_common_values() -> None:
    payload = {
        "page": 2,
        "pages": 3,
        "total": 2,
        "items": [{"id": "b"}, {"id": "a"}],
    }

    assert json_text({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert mapping_items(payload, "items") == [{"id": "b"}, {"id": "a"}]
    assert format_list_ids(payload, "items") == "b\na"
    assert page_line(payload, "items") == "items: 2 (page 2/3)"
    assert human_bytes(1500) == "1.5 KB"


def test_shared_cli_output_formats_lifecycle_event_pages() -> None:
    payload = {
        "events": [
            {
                "id": "event-1",
                "time": "2026-08-02T12:00:00.000000Z",
                "type": "io.riverhog.munchy.job.succeeded",
                "subject": "job-1",
            }
        ],
        "next_cursor": "41",
        "has_more": True,
    }

    assert format_lifecycle_events(payload) == (
        "events: 1\n"
        "2026-08-02T12:00:00.000000Z io.riverhog.munchy.job.succeeded "
        "subject=job-1 id=event-1\n"
        "next cursor: 41\n"
        "has more: yes"
    )
