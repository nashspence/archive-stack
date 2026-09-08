"""Recoverable, authenticated collection-tag authorities."""

from __future__ import annotations

import hashlib
import json
import struct
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

COLLECTION_TAG_HEAD_FORMAT: Literal["riverhog-collection-tag-head/v1"] = (
    "riverhog-collection-tag-head/v1"
)
COLLECTION_TAG_HEAD_RELATIVE_PATH = "tags/head.json.age"
COLLECTION_TAG_NODE_FORMAT = "riverhog-collection-tag-node/v1"
COLLECTION_TAG_NODE_RELATIVE_PREFIX = "tags/nodes"
COLLECTION_TAG_UTF8_BYTES_MAX = 65_536
COLLECTION_TAG_NODE_BYTES_MAX = 128 * 1024
COLLECTION_TAG_PAGE_SIZE_MAX = 1_000
COLLECTION_TAG_PAGE_UTF8_BYTES_MAX = 4 * COLLECTION_TAG_UTF8_BYTES_MAX
COLLECTION_TAG_REQUEST_MEMBERS_MAX = 100
MAX_COLLECTION_TAG_REVISION = 9_007_199_254_740_991

_NODE_MAGIC = b"RHTG"
_NODE_VERSION = 1
_NODE_HEADER = struct.Struct(">4sBIIH")
_NODE_CHILD_BYTES = 33
_NODE_DIGEST_DOMAIN = b"riverhog-collection-tag-node/v1\x00"
_TAG_SET_IDENTITY_DOMAIN = b"riverhog-collection-tag-set/v1\x00"
_TAG_HEAD_IDENTITY_DOMAIN = b"riverhog-collection-tag-head-state/v1\x00"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_UNICODE_WHITESPACE = frozenset(
    "\u0009\u000a\u000b\u000c\u000d\u0020\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
    "\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)


class CollectionTagIntegrityError(ValueError):
    """An authenticated tag-set representation is inconsistent."""


class CollectionTagNodeMissing(CollectionTagIntegrityError):
    """A referenced immutable tag node is unavailable."""


def validate_collection_tag(value: str) -> str:
    """Return one exact canonical tag or reject it without repair."""

    if type(value) is not str:
        raise ValueError("collection tag must be a string")
    if not value:
        raise ValueError("collection tag must not be empty")
    if value[0] in _UNICODE_WHITESPACE or value[-1] in _UNICODE_WHITESPACE:
        raise ValueError("collection tag must not have edge whitespace")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("collection tag must use NFC normalization")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("collection tag must contain Unicode scalar values") from exc
    if len(encoded) > COLLECTION_TAG_UTF8_BYTES_MAX:
        raise ValueError("collection tag exceeds its UTF-8 byte limit")
    for character in value:
        codepoint = ord(character)
        if codepoint <= 0x1F or 0x7F <= codepoint <= 0x9F:
            raise ValueError("collection tag contains a control character")
    return value


type CollectionTag = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=COLLECTION_TAG_UTF8_BYTES_MAX),
    AfterValidator(validate_collection_tag),
    Field(
        json_schema_extra={
            "x-riverhog-encoded-bytes-max": COLLECTION_TAG_UTF8_BYTES_MAX,
            "x-riverhog-extent": {
                "policy": "contract_max",
                "reason": "bounded-human-authored-collection-tag",
            },
            "x-unicode-normalization": "NFC",
        }
    ),
]


def _validate_sha256(value: str, *, label: str = "SHA-256") -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")
    return value


def collection_tag_node_path(digest: str) -> str:
    """Return the archive-relative deterministic path for one encrypted node."""

    canonical = _validate_sha256(digest, label="tag node SHA-256")
    return f"{COLLECTION_TAG_NODE_RELATIVE_PREFIX}/{canonical[:2]}/{canonical[2:]}.age"


@dataclass(frozen=True, slots=True)
class CollectionTagChild:
    label: int
    digest: str


@dataclass(frozen=True, slots=True)
class CollectionTagNode:
    prefix: bytes
    tag: bytes | None = None
    children: tuple[CollectionTagChild, ...] = ()


def collection_tag_node_digest(encoded: bytes) -> str:
    return hashlib.sha256(_NODE_DIGEST_DOMAIN + bytes(encoded)).hexdigest()


def encode_collection_tag_node(node: CollectionTagNode) -> bytes:
    """Encode one bounded radix-tree node canonically."""

    prefix = bytes(node.prefix)
    if len(prefix) > COLLECTION_TAG_UTF8_BYTES_MAX:
        raise ValueError("tag node prefix exceeds the maximum tag size")
    tag = None if node.tag is None else bytes(node.tag)
    if tag is not None:
        try:
            validate_collection_tag(tag.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("tag node value is not a canonical tag") from exc
    if len(node.children) > 256:
        raise ValueError("tag node has more than 256 children")
    if len(prefix) > hashlib.sha256().digest_size:
        raise ValueError("tag node key prefix exceeds SHA-256")
    if tag is None and not node.children:
        raise ValueError("a nonterminal tag node must have a child")

    child_bytes = bytearray()
    previous = -1
    for child in node.children:
        if type(child.label) is not int or not 0 <= child.label <= 255:
            raise ValueError("tag node child label must be an integer byte")
        if child.label <= previous:
            raise ValueError("tag node children must be unique and sorted")
        previous = child.label
        child_bytes.append(child.label)
        child_bytes.extend(bytes.fromhex(_validate_sha256(child.digest)))

    tag_bytes = tag or b""
    encoded = (
        _NODE_HEADER.pack(
            _NODE_MAGIC,
            _NODE_VERSION,
            len(prefix),
            len(tag_bytes),
            len(node.children),
        )
        + prefix
        + tag_bytes
        + child_bytes
    )
    if len(encoded) > COLLECTION_TAG_NODE_BYTES_MAX:
        raise ValueError("encoded tag node exceeds its byte limit")
    return encoded


def decode_collection_tag_node(encoded: bytes) -> CollectionTagNode:
    """Decode and verify one bounded canonical radix-tree node."""

    data = bytes(encoded)
    if len(data) > COLLECTION_TAG_NODE_BYTES_MAX:
        raise ValueError("encoded tag node exceeds its byte limit")
    if len(data) < _NODE_HEADER.size:
        raise ValueError("encoded tag node is shorter than its header")
    magic, version, prefix_length, tag_length, child_count = _NODE_HEADER.unpack_from(data)
    if magic != _NODE_MAGIC or version != _NODE_VERSION:
        raise ValueError("tag node format is unsupported")
    if prefix_length > hashlib.sha256().digest_size:
        raise ValueError("tag node key prefix exceeds SHA-256")
    if child_count > 256:
        raise ValueError("tag node has more than 256 children")
    expected_size = _NODE_HEADER.size + prefix_length + tag_length + child_count * _NODE_CHILD_BYTES
    if expected_size != len(data):
        raise ValueError("tag node length differs from its encoding")

    prefix_end = _NODE_HEADER.size + prefix_length
    prefix = data[_NODE_HEADER.size : prefix_end]
    tag_end = prefix_end + tag_length
    tag = data[prefix_end:tag_end] if tag_length else None
    children: list[CollectionTagChild] = []
    cursor = tag_end
    previous = -1
    for _ in range(child_count):
        label = data[cursor]
        digest = data[cursor + 1 : cursor + _NODE_CHILD_BYTES].hex()
        cursor += _NODE_CHILD_BYTES
        if label <= previous:
            raise ValueError("tag node children must be unique and sorted")
        previous = label
        children.append(CollectionTagChild(label, digest))
    node = CollectionTagNode(prefix, tag, tuple(children))
    if encode_collection_tag_node(node) != data:
        raise ValueError("tag node is not canonically encoded")
    return node


class CollectionTagNodeStore(Protocol):
    """The minimal immutable node storage needed by the tag-set contract."""

    def get(self, digest: str) -> bytes: ...

    def put(self, digest: str, encoded: bytes) -> None: ...


class MemoryCollectionTagNodeStore:
    """A qualification store for the content-addressed node contract."""

    def __init__(self) -> None:
        self.nodes: dict[str, bytes] = {}
        self.get_count = 0
        self.put_count = 0

    def get(self, digest: str) -> bytes:
        canonical = _validate_sha256(digest)
        self.get_count += 1
        try:
            encoded = self.nodes[canonical]
        except KeyError as exc:
            raise CollectionTagNodeMissing(canonical) from exc
        if collection_tag_node_digest(encoded) != canonical:
            raise CollectionTagIntegrityError("stored tag node digest differs")
        return encoded

    def put(self, digest: str, encoded: bytes) -> None:
        canonical = _validate_sha256(digest)
        payload = bytes(encoded)
        if collection_tag_node_digest(payload) != canonical:
            raise CollectionTagIntegrityError("stored tag node digest differs")
        self.put_count += 1
        existing = self.nodes.get(canonical)
        if existing is not None and existing != payload:
            raise CollectionTagIntegrityError("immutable tag node differs")
        self.nodes[canonical] = payload


def collection_tag_set_identity(root_sha256: str | None) -> str:
    if root_sha256 is None:
        payload = b"empty"
    else:
        payload = b"root\x00" + bytes.fromhex(_validate_sha256(root_sha256))
    return hashlib.sha256(_TAG_SET_IDENTITY_DOMAIN + payload).hexdigest()


def collection_tag_sha256(tag: str) -> str:
    """Return the exact canonical tag identity used across public projections."""

    return hashlib.sha256(validate_collection_tag(tag).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CollectionTagSetRoot:
    root_sha256: str | None
    tag_set_identity: str

    @classmethod
    def seal(cls, root_sha256: str | None) -> CollectionTagSetRoot:
        if root_sha256 is not None:
            _validate_sha256(root_sha256)
        return cls(root_sha256, collection_tag_set_identity(root_sha256))


def _common_prefix(left: bytes, right: bytes) -> int:
    count = 0
    while count < min(len(left), len(right)) and left[count] == right[count]:
        count += 1
    return count


def _load_tag_node(store: CollectionTagNodeStore, digest: str) -> CollectionTagNode:
    encoded = store.get(_validate_sha256(digest))
    if collection_tag_node_digest(encoded) != digest:
        raise CollectionTagIntegrityError("tag node digest differs")
    return decode_collection_tag_node(encoded)


def _put_tag_node(store: CollectionTagNodeStore, node: CollectionTagNode) -> str:
    encoded = encode_collection_tag_node(node)
    digest = collection_tag_node_digest(encoded)
    store.put(digest, encoded)
    return digest


def _sorted_children(children: Sequence[CollectionTagChild]) -> tuple[CollectionTagChild, ...]:
    return tuple(sorted(children, key=lambda child: child.label))


def _store_tag_node(
    store: CollectionTagNodeStore,
    prefix: bytes,
    tag: bytes | None,
    children: Sequence[CollectionTagChild] = (),
) -> str:
    return _put_tag_node(store, CollectionTagNode(prefix, tag, _sorted_children(children)))


def _replace_child(
    children: Sequence[CollectionTagChild], label: int, digest: str | None
) -> tuple[CollectionTagChild, ...]:
    values = {child.label: child.digest for child in children}
    if digest is None:
        values.pop(label, None)
    else:
        values[label] = digest
    return tuple(CollectionTagChild(key, values[key]) for key in sorted(values))


def _normalize_tag_node(
    store: CollectionTagNodeStore,
    prefix: bytes,
    tag: bytes | None,
    children: Sequence[CollectionTagChild],
) -> str | None:
    canonical_children = _sorted_children(children)
    if tag is None and not canonical_children:
        return None
    if tag is None and len(canonical_children) == 1:
        child = canonical_children[0]
        node = _load_tag_node(store, child.digest)
        if not node.prefix or node.prefix[0] != child.label:
            raise CollectionTagIntegrityError("tag child label differs from its prefix")
        return _store_tag_node(store, prefix + node.prefix, node.tag, node.children)
    return _store_tag_node(store, prefix, tag, canonical_children)


@dataclass(frozen=True, slots=True)
class _TagAncestor:
    node: CollectionTagNode
    label: int


def _rebuild_tag_path(
    store: CollectionTagNodeStore,
    ancestors: list[_TagAncestor],
    replacement: str | None,
) -> str | None:
    current = replacement
    for ancestor in reversed(ancestors):
        current = _normalize_tag_node(
            store,
            ancestor.node.prefix,
            ancestor.node.tag,
            _replace_child(ancestor.node.children, ancestor.label, current),
        )
    return current


class CollectionTagSet:
    """A copy-on-write authenticated radix set over fixed SHA-256 tag paths."""

    def __init__(
        self,
        store: CollectionTagNodeStore,
        root: CollectionTagSetRoot | None = None,
    ) -> None:
        self.store = store
        self.root = root or CollectionTagSetRoot.seal(None)
        if self.root != CollectionTagSetRoot.seal(self.root.root_sha256):
            raise CollectionTagIntegrityError("tag-set root is inconsistent")

    @property
    def identity(self) -> str:
        return self.root.tag_set_identity

    def contains(self, value: str) -> bool:
        tag = validate_collection_tag(value).encode("utf-8")
        remaining = hashlib.sha256(tag).digest()
        current = self.root.root_sha256
        expected_label: int | None = None
        seen: set[str] = set()
        while current is not None:
            if current in seen:
                raise CollectionTagIntegrityError("tag path contains a cycle")
            seen.add(current)
            node = _load_tag_node(self.store, current)
            if expected_label is not None and (not node.prefix or node.prefix[0] != expected_label):
                raise CollectionTagIntegrityError("tag child label differs from its prefix")
            common = _common_prefix(node.prefix, remaining)
            if common != len(node.prefix):
                return False
            remaining = remaining[common:]
            if not remaining:
                if node.tag is not None and node.tag != tag:
                    raise CollectionTagIntegrityError("distinct tags have the same SHA-256 path")
                return node.tag == tag
            expected_label = remaining[0]
            edge = next((child for child in node.children if child.label == expected_label), None)
            if edge is None:
                return False
            current = edge.digest
        return False

    def insert(self, value: str) -> CollectionTagSet:
        tag = validate_collection_tag(value).encode("utf-8")
        key = hashlib.sha256(tag).digest()
        if self.root.root_sha256 is None:
            return CollectionTagSet(
                self.store, CollectionTagSetRoot.seal(_store_tag_node(self.store, key, tag))
            )
        ancestors: list[_TagAncestor] = []
        current = self.root.root_sha256
        remaining = key
        seen: set[str] = set()
        expected_label: int | None = None
        while True:
            if current in seen:
                raise CollectionTagIntegrityError("tag mutation path contains a cycle")
            seen.add(current)
            node = _load_tag_node(self.store, current)
            if expected_label is not None and (not node.prefix or node.prefix[0] != expected_label):
                raise CollectionTagIntegrityError("tag child label differs from its prefix")
            common = _common_prefix(node.prefix, remaining)
            if common < len(node.prefix):
                old_suffix = node.prefix[common:]
                old_digest = _store_tag_node(self.store, old_suffix, node.tag, node.children)
                new_suffix = remaining[common:]
                children = [CollectionTagChild(old_suffix[0], old_digest)]
                terminal_tag = tag if not new_suffix else None
                if new_suffix:
                    children.append(
                        CollectionTagChild(
                            new_suffix[0], _store_tag_node(self.store, new_suffix, tag)
                        )
                    )
                replacement = _store_tag_node(
                    self.store, node.prefix[:common], terminal_tag, children
                )
                root = _rebuild_tag_path(self.store, ancestors, replacement)
                return CollectionTagSet(self.store, CollectionTagSetRoot.seal(root))
            remaining = remaining[common:]
            if not remaining:
                if node.tag == tag:
                    return self
                if node.tag is not None:
                    raise CollectionTagIntegrityError("distinct tags have the same SHA-256 path")
                replacement = _store_tag_node(self.store, node.prefix, tag, node.children)
                return CollectionTagSet(
                    self.store,
                    CollectionTagSetRoot.seal(
                        _rebuild_tag_path(self.store, ancestors, replacement)
                    ),
                )
            label = remaining[0]
            edge = next((child for child in node.children if child.label == label), None)
            if edge is None:
                leaf = _store_tag_node(self.store, remaining, tag)
                replacement = _store_tag_node(
                    self.store,
                    node.prefix,
                    node.tag,
                    (*node.children, CollectionTagChild(label, leaf)),
                )
                root = _rebuild_tag_path(self.store, ancestors, replacement)
                return CollectionTagSet(self.store, CollectionTagSetRoot.seal(root))
            ancestors.append(_TagAncestor(node, label))
            current = edge.digest
            expected_label = label

    def discard(self, value: str) -> CollectionTagSet:
        tag = validate_collection_tag(value).encode("utf-8")
        key = hashlib.sha256(tag).digest()
        if self.root.root_sha256 is None:
            return self
        ancestors: list[_TagAncestor] = []
        current = self.root.root_sha256
        remaining = key
        seen: set[str] = set()
        expected_label: int | None = None
        while True:
            if current in seen:
                raise CollectionTagIntegrityError("tag mutation path contains a cycle")
            seen.add(current)
            node = _load_tag_node(self.store, current)
            if expected_label is not None and (not node.prefix or node.prefix[0] != expected_label):
                raise CollectionTagIntegrityError("tag child label differs from its prefix")
            common = _common_prefix(node.prefix, remaining)
            if common != len(node.prefix):
                return self
            remaining = remaining[common:]
            if not remaining:
                if node.tag is None:
                    return self
                if node.tag != tag:
                    raise CollectionTagIntegrityError("distinct tags have the same SHA-256 path")
                replacement = _normalize_tag_node(self.store, node.prefix, None, node.children)
                root = _rebuild_tag_path(self.store, ancestors, replacement)
                return CollectionTagSet(self.store, CollectionTagSetRoot.seal(root))
            label = remaining[0]
            edge = next((child for child in node.children if child.label == label), None)
            if edge is None:
                return self
            ancestors.append(_TagAncestor(node, label))
            current = edge.digest
            expected_label = label

    def iter_tags(self, *, start_after_sha256: str | None = None) -> Iterator[str]:
        start = None
        if start_after_sha256 is not None:
            _validate_sha256(start_after_sha256, label="tag page position")
            start = bytes.fromhex(start_after_sha256)
        if self.root.root_sha256 is None:
            return
        stack: list[tuple[bool, str, bytes, int | None]] = [
            (True, self.root.root_sha256, b"", None)
        ]
        active: set[str] = set()
        while stack:
            entering, digest, base, expected_label = stack.pop()
            if not entering:
                active.remove(digest)
                continue
            if digest in active:
                raise CollectionTagIntegrityError("tag representation contains a cycle")
            active.add(digest)
            node = _load_tag_node(self.store, digest)
            if expected_label is not None and (not node.prefix or node.prefix[0] != expected_label):
                raise CollectionTagIntegrityError("tag child label differs from its prefix")
            full = base + node.prefix
            if len(full) > hashlib.sha256().digest_size:
                raise CollectionTagIntegrityError("tag tree path exceeds SHA-256")
            if start is not None and full < start[: len(full)]:
                active.remove(digest)
                continue
            stack.append((False, digest, base, expected_label))
            for child in reversed(node.children):
                if (
                    start is not None
                    and full == start[: len(full)]
                    and len(full) < len(start)
                    and child.label < start[len(full)]
                ):
                    continue
                stack.append((True, child.digest, full, child.label))
            if node.tag is not None:
                if len(full) != hashlib.sha256().digest_size:
                    raise CollectionTagIntegrityError("tag value is stored before a complete key")
                try:
                    tag = node.tag.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise CollectionTagIntegrityError("tag bytes are not UTF-8") from exc
                try:
                    validate_collection_tag(tag)
                except ValueError as exc:
                    raise CollectionTagIntegrityError("tag bytes are not canonical") from exc
                if hashlib.sha256(node.tag).digest() != full:
                    raise CollectionTagIntegrityError("tag value differs from its SHA-256 path")
                if start is None or full > start:
                    yield tag


def collection_tag_head_identity(
    *,
    archive_root_sha256: str,
    revision: int,
    root_sha256: str | None,
    tag_set_identity: str,
) -> str:
    _validate_sha256(archive_root_sha256, label="archive root SHA-256")
    if type(revision) is not int or not 1 <= revision <= MAX_COLLECTION_TAG_REVISION:
        raise ValueError("collection tag revision is invalid")
    if root_sha256 is not None:
        _validate_sha256(root_sha256, label="tag root SHA-256")
    _validate_sha256(tag_set_identity, label="tag-set identity")
    payload = json.dumps(
        {
            "archive_root_sha256": archive_root_sha256,
            "format": COLLECTION_TAG_HEAD_FORMAT,
            "revision": revision,
            "root_sha256": root_sha256,
            "tag_set_identity": tag_set_identity,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(_TAG_HEAD_IDENTITY_DOMAIN + payload).hexdigest()


class CollectionTagHeadDocument(BaseModel):
    """Canonical mutable head for one recoverable collection tag authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["riverhog-collection-tag-head/v1"] = COLLECTION_TAG_HEAD_FORMAT
    archive_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    revision: int = Field(ge=1, le=MAX_COLLECTION_TAG_REVISION, strict=True)
    root_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    tag_set_identity: str = Field(pattern=_SHA256_PATTERN)
    head_identity: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_identities(self) -> Self:
        expected_set = collection_tag_set_identity(self.root_sha256)
        if self.tag_set_identity != expected_set:
            raise ValueError("collection tag-set identity differs from its root")
        expected_head = collection_tag_head_identity(
            archive_root_sha256=self.archive_root_sha256,
            revision=self.revision,
            root_sha256=self.root_sha256,
            tag_set_identity=self.tag_set_identity,
        )
        if self.head_identity != expected_head:
            raise ValueError("collection tag-head identity differs from its state")
        return self

    @classmethod
    def seal(
        cls,
        *,
        archive_root_sha256: str,
        revision: int,
        root_sha256: str | None,
    ) -> CollectionTagHeadDocument:
        set_identity = collection_tag_set_identity(root_sha256)
        return cls(
            archive_root_sha256=archive_root_sha256,
            revision=revision,
            root_sha256=root_sha256,
            tag_set_identity=set_identity,
            head_identity=collection_tag_head_identity(
                archive_root_sha256=archive_root_sha256,
                revision=revision,
                root_sha256=root_sha256,
                tag_set_identity=set_identity,
            ),
        )

    def to_json_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")

    @classmethod
    def from_json_bytes(cls, content: bytes | str) -> CollectionTagHeadDocument:
        encoded = content if isinstance(content, bytes) else content.encode("ascii")
        try:
            document = cls.model_validate_json(encoded)
        except (UnicodeError, ValueError) as exc:
            raise ValueError("collection tag-head document is invalid") from exc
        if document.to_json_bytes() != encoded:
            raise ValueError("collection tag-head document is not canonical")
        return document


__all__ = [
    "COLLECTION_TAG_HEAD_FORMAT",
    "COLLECTION_TAG_HEAD_RELATIVE_PATH",
    "COLLECTION_TAG_NODE_BYTES_MAX",
    "COLLECTION_TAG_NODE_FORMAT",
    "COLLECTION_TAG_NODE_RELATIVE_PREFIX",
    "COLLECTION_TAG_PAGE_SIZE_MAX",
    "COLLECTION_TAG_PAGE_UTF8_BYTES_MAX",
    "COLLECTION_TAG_REQUEST_MEMBERS_MAX",
    "COLLECTION_TAG_UTF8_BYTES_MAX",
    "MAX_COLLECTION_TAG_REVISION",
    "CollectionTag",
    "CollectionTagChild",
    "CollectionTagHeadDocument",
    "CollectionTagIntegrityError",
    "CollectionTagNode",
    "CollectionTagNodeMissing",
    "CollectionTagNodeStore",
    "CollectionTagSet",
    "CollectionTagSetRoot",
    "MemoryCollectionTagNodeStore",
    "collection_tag_head_identity",
    "collection_tag_node_digest",
    "collection_tag_node_path",
    "collection_tag_sha256",
    "collection_tag_set_identity",
    "decode_collection_tag_node",
    "encode_collection_tag_node",
    "validate_collection_tag",
]
