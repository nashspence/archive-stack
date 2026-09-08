from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import TypeAdapter, ValidationError
from riverhog_protocol import (
    COLLECTION_TAG_NODE_BYTES_MAX,
    COLLECTION_TAG_UTF8_BYTES_MAX,
    CollectionTag,
    CollectionTagHeadDocument,
    CollectionTagIntegrityError,
    CollectionTagSet,
    CollectionTagSetRoot,
    MemoryCollectionTagNodeStore,
    collection_tag_node_digest,
    collection_tag_node_path,
)

TAG = TypeAdapter(CollectionTag)
ROOT = "a" * 64


def test_collection_tag_contract_accepts_exact_case_sensitive_nfc_unicode() -> None:
    assert TAG.validate_python("場所/東京") == "場所/東京"
    assert TAG.validate_python("Camera") != TAG.validate_python("camera")
    assert TAG.validate_python("a" * COLLECTION_TAG_UTF8_BYTES_MAX)
    assert TAG.validate_python("🦆" * (COLLECTION_TAG_UTF8_BYTES_MAX // 4))


@pytest.mark.parametrize(
    "value",
    (
        "",
        " leading",
        "trailing\u3000",
        "line\nbreak",
        "e\u0301",
        "a" * (COLLECTION_TAG_UTF8_BYTES_MAX + 1),
        "🦆" * (COLLECTION_TAG_UTF8_BYTES_MAX // 4 + 1),
    ),
)
def test_collection_tag_contract_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(ValidationError):
        TAG.validate_python(value)


def test_copy_on_write_radix_set_is_deterministic_and_does_not_rewrite_old_nodes() -> None:
    store = MemoryCollectionTagNodeStore()
    first = CollectionTagSet(store).insert("camera/front").insert("場所/東京")
    first_nodes = dict(store.nodes)

    equivalent = CollectionTagSet(MemoryCollectionTagNodeStore())
    equivalent = equivalent.insert("場所/東京").insert("camera/front")
    assert equivalent.identity == first.identity
    assert equivalent.root == first.root

    second = first.insert("camera/rear")
    assert second.identity != first.identity
    assert all(store.nodes[digest] == encoded for digest, encoded in first_nodes.items())
    assert first.contains("camera/front")
    assert not first.contains("camera/rear")
    assert second.contains("camera/rear")
    expected = sorted(
        ("camera/front", "camera/rear", "場所/東京"),
        key=lambda tag: hashlib.sha256(tag.encode("utf-8")).digest(),
    )
    assert list(second.iter_tags()) == expected


def test_empty_tag_set_is_an_explicit_authenticated_state() -> None:
    empty = CollectionTagSet(MemoryCollectionTagNodeStore())
    assert empty.root == CollectionTagSetRoot.seal(None)
    assert len(empty.identity) == 64
    assert list(empty.iter_tags()) == []


def test_single_mutation_reads_only_the_changed_path() -> None:
    store = MemoryCollectionTagNodeStore()
    current = CollectionTagSet(store)
    for index in range(2_000):
        current = current.insert(f"camera/{index:08d}")
    store.get_count = 0
    changed = current.insert("camera/00001000/derived")
    assert changed.contains("camera/00001000/derived")
    assert store.get_count < 40


def test_maximum_tag_fits_inside_the_independently_bounded_node_envelope() -> None:
    store = MemoryCollectionTagNodeStore()
    tags = CollectionTagSet(store).insert("a" * COLLECTION_TAG_UTF8_BYTES_MAX)
    assert tags.contains("a" * COLLECTION_TAG_UTF8_BYTES_MAX)
    assert max(map(len, store.nodes.values())) <= COLLECTION_TAG_NODE_BYTES_MAX


def test_tag_head_binds_archive_revision_and_exact_set() -> None:
    store = MemoryCollectionTagNodeStore()
    tags = CollectionTagSet(store).insert("workflow/camera")
    head = CollectionTagHeadDocument.seal(
        archive_root_sha256=ROOT,
        revision=7,
        root_sha256=tags.root.root_sha256,
    )
    assert CollectionTagHeadDocument.from_json_bytes(head.to_json_bytes()) == head
    assert head.tag_set_identity == tags.identity
    assert head.to_json_bytes() == json.dumps(
        head.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    assert collection_tag_node_path(tags.root.root_sha256 or "").startswith("tags/nodes/")


def test_tag_node_authentication_fails_closed() -> None:
    store = MemoryCollectionTagNodeStore()
    tags = CollectionTagSet(store).insert("camera/front")
    root = tags.root.root_sha256
    assert root is not None
    store.nodes[root] = store.nodes[root][:-1] + bytes([store.nodes[root][-1] ^ 1])
    with pytest.raises(CollectionTagIntegrityError):
        tags.contains("camera/front")
    assert collection_tag_node_digest(store.nodes[root]) != root
