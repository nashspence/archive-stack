"""Capability-scoped reads of exact immutable claimed collection artifacts."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Literal, Protocol, Self

from riverhog_protocol.collection_workflows import (
    DERIVATION_EVIDENCE_PATH,
    PRODUCER_EVIDENCE_PATH,
    CollectionRootIdentity,
)
from riverhog_protocol.paths import CollectionId
from riverhog_protocol.portable_collection import PortableCollectionInventoryPage

from riverhog_transform_sdk.models import ClaimedArtifact

Heartbeat = Callable[[], None]
_CONTROL_PATHS = frozenset({PRODUCER_EVIDENCE_PATH, DERIVATION_EVIDENCE_PATH})
_TERMINAL_RETRIEVAL_STATES = frozenset({"completed", "expired", "failed", "canceled"})
RetrievalPolicy = Literal["available-only", "allow"]
RiverhogRestorePolicy = Literal["never", "allow"]


class ClaimedCollectionApi(Protocol):
    def get_collection(self, collection_id: CollectionId) -> dict[str, Any]: ...

    def get_portable_collection_inventory(
        self,
        collection_id: CollectionId,
        *,
        cursor: str | None = None,
        limit: int = 100,
        inventory_identity: str | None = None,
    ) -> PortableCollectionInventoryPage: ...

    def plan_retrieval(
        self,
        files: Sequence[tuple[CollectionId, str]],
        *,
        lease_seconds: int | None = None,
        restore_policy: RiverhogRestorePolicy = "never",
    ) -> dict[str, Any]: ...

    def create_retrieval_job(
        self,
        plan_id: str,
        *,
        plan_etag: str,
        event_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def list_retrieval_plan_files(
        self,
        plan_id: str,
        *,
        plan_etag: str,
        start_ordinal: int = 0,
        page_size: int = 100,
    ) -> dict[str, Any]: ...

    def get_retrieval_job(self, job_id: str) -> dict[str, Any]: ...

    def renew_retrieval_job(self, job_id: str, *, lease_seconds: int) -> dict[str, Any]: ...

    def acknowledge_retrieval_job(self, job_id: str) -> dict[str, Any]: ...

    def cancel_retrieval_job(self, job_id: str) -> dict[str, Any]: ...

    def download_retrieval_file(
        self,
        job_id: str,
        *,
        collection_id: CollectionId,
        path: str,
        output: Path,
        expected_bytes: int,
        expected_sha256: str,
    ) -> int: ...

    def stream_retrieval_file(
        self,
        job_id: str,
        *,
        collection_id: CollectionId,
        path: str,
        expected_bytes: int,
        expected_sha256: str,
        start: int = 0,
        end: int | None = None,
        chunk_size: int = 8 * 1024 * 1024,
    ) -> AbstractContextManager[Iterator[bytes]]: ...


class ClaimedCollectionReader:
    """Resolve and read only the immutable roots sealed into one transform intent."""

    def __init__(
        self,
        api: ClaimedCollectionApi,
        *,
        inputs: Sequence[CollectionRootIdentity],
        work_id: str,
        claim_id: str,
        fence: int,
        heartbeat: Heartbeat | None = None,
    ) -> None:
        if not claim_id or claim_id != claim_id.strip():
            raise ValueError("claimed collection reader requires a canonical claim id")
        if isinstance(fence, bool) or fence < 1:
            raise ValueError("claimed collection reader requires a positive fence")
        normalized_inputs = tuple(sorted(inputs))
        if not normalized_inputs or normalized_inputs != tuple(inputs):
            raise ValueError("claimed collection inputs must be nonempty and canonical")
        if len({item.collection_id for item in normalized_inputs}) != len(normalized_inputs):
            raise ValueError("claimed collection inputs must be unique")
        normalized_work_id = work_id.casefold()
        if len(normalized_work_id) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_work_id
        ):
            raise ValueError("claimed collection reader requires a work SHA-256 identity")
        self.api = api
        self.inputs = normalized_inputs
        self.work_id = normalized_work_id
        self.claim_id = claim_id
        self.fence = fence
        self.heartbeat = heartbeat

    def replace_api(self, api: ClaimedCollectionApi) -> None:
        self.api = api

    def inventory(self, *, include_control: bool = False) -> tuple[ClaimedArtifact, ...]:
        return tuple(self.iter_inventory(include_control=include_control))

    def iter_inventory(self, *, include_control: bool = False) -> Iterator[ClaimedArtifact]:
        previous: ClaimedArtifact | None = None
        for root in self.inputs:
            self._verify_root(root)
            cursor: str | None = None
            inventory_identity: str | None = None
            while True:
                page = self.api.get_portable_collection_inventory(
                    root.collection_id,
                    cursor=cursor,
                    limit=1000,
                    inventory_identity=inventory_identity,
                )
                if inventory_identity is None:
                    inventory_identity = page.authority.inventory_identity
                elif page.authority.inventory_identity != inventory_identity:
                    raise RuntimeError("claimed collection inventory identity changed")
                for item in page.files:
                    path = item.path
                    control = path in _CONTROL_PATHS or path.startswith("riverhog/")
                    artifact = ClaimedArtifact(
                        root=root,
                        path=path,
                        bytes=item.bytes,
                        sha256=item.sha256,
                        control=control,
                    )
                    if include_control or not control:
                        if previous is not None and artifact <= previous:
                            raise RuntimeError(
                                "claimed collection inventory is not canonical and unique"
                            )
                        previous = artifact
                        yield artifact
                if page.complete:
                    break
                cursor = page.next_cursor

    def prepare(
        self,
        artifacts: Sequence[ClaimedArtifact] | None = None,
        *,
        lease_seconds: int = 1800,
        restore_policy: RetrievalPolicy = "available-only",
        poll_seconds: float = 2.0,
        timeout_seconds: float = 24 * 60 * 60,
    ) -> ClaimedRetrieval:
        if lease_seconds < 1:
            raise ValueError("retrieval lease must be positive")
        if artifacts is None:
            raise ValueError("claimed retrieval requires an explicit bounded artifact selection")
        selected = tuple(sorted(artifacts))
        if not selected:
            raise ValueError("claimed retrieval requires at least one artifact")
        if len({current.key for current in selected}) != len(selected):
            raise ValueError("claimed retrieval artifacts must be unique")
        refs = [(current.root.collection_id, current.path) for current in selected]
        plan = self.api.plan_retrieval(
            refs,
            lease_seconds=lease_seconds,
            restore_policy=("never" if restore_policy == "available-only" else "allow"),
        )
        plan_etag = str(plan.get("etag") or "")
        if len(plan_etag) != 64:
            raise RuntimeError("Riverhog retrieval plan has no stable identity")
        _verify_plan_files(
            _retrieval_plan_files(self.api, plan),
            selected,
        )
        job = self.api.create_retrieval_job(
            str(plan["id"]),
            plan_etag=plan_etag,
            event_context={
                "initiator": {
                    "app": "collection-work",
                    "claim_id": self.claim_id,
                    "fence": self.fence,
                    "work_id": self.work_id,
                }
            },
        )
        deadline = time.monotonic() + timeout_seconds
        while str(job.get("state") or "") == "requested":
            if time.monotonic() >= deadline:
                _best_effort_cancel(self.api, str(job.get("id") or ""))
                raise TimeoutError("claimed collection retrieval did not become ready")
            if self.heartbeat is not None:
                self.heartbeat()
            time.sleep(max(0.05, poll_seconds))
            job = self.api.get_retrieval_job(str(job["id"]))
        state = str(job.get("state") or "")
        if state != "ready":
            failure = str(job.get("failure") or state or "unknown retrieval failure")
            raise RuntimeError(f"claimed collection retrieval is not ready: {failure}")
        _verify_job(job, plan_id=str(plan["id"]), plan_etag=plan_etag)
        return ClaimedRetrieval(
            self.api,
            job=job,
            artifacts=selected,
            heartbeat=self.heartbeat,
        )

    def _verify_root(self, expected: CollectionRootIdentity) -> None:
        payload = self.api.get_collection(expected.collection_id)
        actual = CollectionRootIdentity(
            collection_id=_positive_int(payload.get("id"), "collection id"),
            archive_root_sha256=str(payload.get("archive_root_sha256") or ""),
            content_identity=str(payload.get("content_identity") or ""),
        )
        if actual != expected:
            raise RuntimeError(f"claimed collection root changed: {expected.collection_id}")


class ClaimedRetrieval:
    """A ready Riverhog retrieval job bound to exact claimed artifact identities."""

    def __init__(
        self,
        api: ClaimedCollectionApi,
        *,
        job: Mapping[str, Any],
        artifacts: Sequence[ClaimedArtifact],
        heartbeat: Heartbeat | None = None,
    ) -> None:
        job_id = str(job.get("id") or "")
        if not job_id:
            raise ValueError("claimed retrieval requires a job id")
        self.api = api
        self.job = dict(job)
        self.job_id = job_id
        self.artifacts = tuple(sorted(artifacts))
        self._by_key = {current.key: current for current in self.artifacts}
        self.heartbeat = heartbeat
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> None:
        self.close(success=exc_type is None)

    @property
    def closed(self) -> bool:
        return self._closed

    def replace_api(self, api: ClaimedCollectionApi) -> None:
        if self._closed:
            raise RuntimeError("claimed retrieval is already closed")
        self.api = api

    @contextmanager
    def stream(
        self,
        artifact: ClaimedArtifact,
        *,
        start: int = 0,
        end: int | None = None,
        chunk_size: int = 8 * 1024 * 1024,
    ) -> Iterator[Iterator[bytes]]:
        current = self._require_artifact(artifact)
        if self.heartbeat is not None:
            self.heartbeat()
        with self.api.stream_retrieval_file(
            self.job_id,
            collection_id=current.root.collection_id,
            path=current.path,
            expected_bytes=current.bytes,
            expected_sha256=current.sha256,
            start=start,
            end=end,
            chunk_size=chunk_size,
        ) as chunks:
            yield chunks
        if self.heartbeat is not None:
            self.heartbeat()

    def read_bytes(self, artifact: ClaimedArtifact, *, maximum_bytes: int) -> bytes:
        current = self._require_artifact(artifact)
        if current.bytes > maximum_bytes:
            raise ValueError("claimed artifact exceeds the in-memory read limit")
        with self.stream(current) as chunks:
            return b"".join(chunks)

    def download(self, artifact: ClaimedArtifact, output: Path) -> int:
        current = self._require_artifact(artifact)
        if self.heartbeat is not None:
            self.heartbeat()
        accepted = self.api.download_retrieval_file(
            self.job_id,
            collection_id=current.root.collection_id,
            path=current.path,
            output=output,
            expected_bytes=current.bytes,
            expected_sha256=current.sha256,
        )
        if accepted != current.bytes:
            raise RuntimeError("claimed artifact download returned an inconsistent byte count")
        if self.heartbeat is not None:
            self.heartbeat()
        return accepted

    def renew(self, *, lease_seconds: int) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("claimed retrieval is already closed")
        self.job = self.api.renew_retrieval_job(
            self.job_id,
            lease_seconds=lease_seconds,
        )
        if str(self.job.get("state") or "") != "ready":
            raise RuntimeError("claimed retrieval renewal did not preserve readiness")
        return dict(self.job)

    def close(self, *, success: bool = True) -> None:
        if self._closed:
            return
        state = str(self.job.get("state") or "")
        if state in _TERMINAL_RETRIEVAL_STATES:
            self._closed = True
            return
        result = (
            self.api.acknowledge_retrieval_job(self.job_id)
            if success
            else self.api.cancel_retrieval_job(self.job_id)
        )
        expected = "completed" if success else "canceled"
        if str(result.get("state") or "") != expected:
            raise RuntimeError(f"Riverhog retrieval close did not reach {expected}")
        self.job = result
        self._closed = True

    def _require_artifact(self, artifact: ClaimedArtifact) -> ClaimedArtifact:
        if self._closed:
            raise RuntimeError("claimed retrieval is already closed")
        current = self._by_key.get(artifact.key)
        if current != artifact:
            raise ValueError("artifact is not part of this claimed retrieval")
        return current


def _retrieval_plan_files(
    api: ClaimedCollectionApi,
    plan: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    plan_id = str(plan.get("id") or "")
    plan_etag = str(plan.get("etag") or "")
    file_count = _positive_int(plan.get("file_count"), "plan file count")
    rows: list[Mapping[str, Any]] = []
    start_ordinal = 0
    while True:
        page = api.list_retrieval_plan_files(
            plan_id,
            plan_etag=plan_etag,
            start_ordinal=start_ordinal,
            page_size=100,
        )
        current = page.get("files")
        if (
            page.get("plan_id") != plan_id
            or page.get("etag") != plan_etag
            or page.get("start_ordinal") != start_ordinal
            or not isinstance(current, list)
            or any(not isinstance(item, Mapping) for item in current)
        ):
            raise RuntimeError("Riverhog retrieval plan file page changed its authority")
        rows.extend(current)
        if len(rows) > file_count:
            raise RuntimeError("Riverhog retrieval plan exceeded its declared file count")
        complete = page.get("complete")
        if not isinstance(complete, bool):
            raise RuntimeError("Riverhog retrieval plan page omitted completion state")
        if complete:
            if page.get("next_ordinal") is not None or len(rows) != file_count:
                raise RuntimeError("Riverhog retrieval plan traversal ended inconsistently")
            return tuple(rows)
        next_ordinal = page.get("next_ordinal")
        expected_next = start_ordinal + len(current)
        if not current or isinstance(next_ordinal, bool) or next_ordinal != expected_next:
            raise RuntimeError("Riverhog retrieval plan did not advance exactly")
        start_ordinal = expected_next


def _verify_plan_files(
    rows: Sequence[Mapping[str, Any]],
    selected: Sequence[ClaimedArtifact],
) -> None:
    expected = {current.key: (current.bytes, current.sha256) for current in selected}
    actual: dict[tuple[int, str], tuple[int, str]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise RuntimeError("Riverhog retrieval plan file is invalid")
        key = (
            _positive_int(row.get("collection_id"), "plan collection id"),
            str(row.get("path") or ""),
        )
        if key in actual:
            raise RuntimeError("Riverhog retrieval plan repeats an artifact")
        actual[key] = (
            _nonnegative_int(row.get("bytes"), "plan artifact bytes"),
            str(row.get("sha256") or ""),
        )
    if actual != expected:
        raise RuntimeError("Riverhog retrieval plan differs from the claimed artifacts")


def _verify_job(
    job: Mapping[str, Any],
    *,
    plan_id: str,
    plan_etag: str,
) -> None:
    if str(job.get("plan_id") or "") != plan_id:
        raise RuntimeError("Riverhog retrieval job changed its plan authority")
    if str(job.get("plan_etag") or "") != plan_etag:
        raise RuntimeError("Riverhog retrieval job changed its plan identity")


def _best_effort_cancel(api: ClaimedCollectionApi, job_id: str) -> None:
    if not job_id:
        return
    try:
        api.cancel_retrieval_job(job_id)
    except Exception:
        pass


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeError(f"{label} is invalid")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"{label} is invalid")
    return value


__all__ = [
    "ClaimedCollectionApi",
    "ClaimedCollectionReader",
    "ClaimedRetrieval",
    "Heartbeat",
]
