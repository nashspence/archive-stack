from cli_support.output import format_list_ids, human_bytes, json_text, mapping_items, page_line


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
