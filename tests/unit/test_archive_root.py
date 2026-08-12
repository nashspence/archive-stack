from __future__ import annotations

import hashlib
from dataclasses import dataclass

from riverhog_core.archive_root import ArchiveRootPublisher
from riverhog_core.domain.archive import (
    ArchiveFile,
    SealedPackVolume,
    StoredPartReceipt,
)
from riverhog_core.pack_volume import iter_render_pack_upload_unit, plan_pack_volume
from riverhog_core.ports.archive_objects import ImmutableObjectReceipt

from tests.fixtures.archive import age_state_json


@dataclass
class _Stored:
    content: bytes
    identity: dict[str, str]
    receipt: ImmutableObjectReceipt


class MemoryImmutableStore:
    def __init__(self) -> None:
        self.objects: dict[str, _Stored] = {}

    def put_immutable_object(
        self,
        *,
        object_path: str,
        content: bytes,
        content_type: str,
        identity_metadata: dict[str, str],
    ) -> ImmutableObjectReceipt:
        assert content_type
        existing = self.objects.get(object_path)
        if existing is not None:
            if existing.identity != identity_metadata:
                raise RuntimeError("identity conflict")
            return existing.receipt
        receipt = ImmutableObjectReceipt(
            object_path=object_path,
            version_id="v1",
            etag="etag",
            stored_bytes=len(content),
            stored_sha256=hashlib.sha256(content).hexdigest(),
            completed_at="2026-08-03T00:00:00Z",
        )
        self.objects[object_path] = _Stored(content, dict(identity_metadata), receipt)
        return receipt


def test_root_publish_is_logically_idempotent_and_never_rewrites_manifest() -> None:
    content = b"alpha"
    file = ArchiveFile(
        path="alpha.txt",
        bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    plan = plan_pack_volume((file,), sequence=0)
    plaintext = b"".join(iter_render_pack_upload_unit(plan, 0, lambda _path: (content,)))
    pack = SealedPackVolume(
        volume_id=plan.volume_id,
        sequence=0,
        relative_path=f"volumes/{plan.volume_id}.tar.age",
        files=1,
        source_bytes=len(content),
        plaintext_bytes=plan.plaintext_bytes,
        age_state_json=age_state_json(plan.plaintext_bytes),
        index_sha256=plan.index_sha256,
        plan_sha256=plan.plan_sha256,
        parts=(
            StoredPartReceipt(
                number=1,
                plaintext_start=0,
                plaintext_bytes=len(plaintext),
                plaintext_sha256=hashlib.sha256(plaintext).hexdigest(),
                stored_bytes=len(plaintext) + 1,
                stored_sha256=hashlib.sha256(b"x" + plaintext).hexdigest(),
                etag="etag",
            ),
        ),
        version_id="pack-v1",
        completed_at="2026-08-03T00:00:00Z",
    )
    store = MemoryImmutableStore()
    publisher = ArchiveRootPublisher(
        object_store=store,
        passphrase="archive passphrase",
        scrypt_log_n=1,
    )

    first = publisher.publish(
        archive_storage_prefix="archives/opaque",
        files=(file,),
        packs=((plan, pack),),
    )
    second = publisher.publish(
        archive_storage_prefix="archives/opaque",
        files=(file,),
        packs=((plan, pack),),
    )

    assert first.manifest_bytes == second.manifest_bytes
    assert first.stored_sha256 == second.stored_sha256
    assert len(store.objects) == 1
