"""Read-only capability runtime supplied to content observers."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, Protocol, Self, cast

from riverhog_api_client import ApiClient
from riverhog_transform_sdk import (
    CapabilityApiClient,
    ClaimedArtifact,
    ClaimedCollectionReader,
    ClaimedRetrieval,
    TransformWorkspace,
)
from stove0_observer_protocol import (
    ArtifactSubject,
    ObservationInvocation,
    ObservationRequest,
    ObservationResult,
    ObserverDescriptor,
)

CancellationCheck = Callable[[], None]
Heartbeat = Callable[[], None]


class ContentObserver(Protocol):
    """Transport-neutral observer implementation boundary."""

    def descriptor(self) -> ObserverDescriptor: ...

    def observe(
        self,
        request: ObservationRequest,
        runtime: ObservationRuntime,
    ) -> ObservationResult: ...


class ObservationRuntime:
    """One read-only observation over exact immutable Riverhog artifacts."""

    def __init__(
        self,
        api: Any,
        *,
        request: ObservationRequest,
        claim_id: str,
        fence: int,
        cancellation_check: CancellationCheck | None = None,
        heartbeat: Heartbeat | None = None,
        workspace_assurance: str = "ephemeral",
        owned_api: bool = False,
    ) -> None:
        self.api = (
            api
            if isinstance(api, CapabilityApiClient)
            else CapabilityApiClient(api, owns_client=owned_api)
        )
        self.request = request
        self.claim_id = claim_id.strip()
        self.fence = int(fence)
        if not self.claim_id or self.fence < 1:
            raise ValueError("observation runtime requires a live claim generation")
        self.cancellation_check = cancellation_check
        self.external_heartbeat = heartbeat
        if workspace_assurance not in {"encrypted", "ephemeral"}:
            raise ValueError("observer workspace must be encrypted or ephemeral")
        self.workspace_assurance = cast(Literal["encrypted", "ephemeral"], workspace_assurance)
        roots = tuple(sorted({subject.collection.to_identity() for subject in request.subjects}))
        self.reader = ClaimedCollectionReader(
            self.api,
            inputs=roots,
            work_id=request.work_id,
            claim_id=self.claim_id,
            fence=self.fence,
            heartbeat=self.heartbeat,
        )
        self._retrievals: list[ClaimedRetrieval] = []
        self._closed = False

    @classmethod
    def from_invocation(
        cls,
        invocation: ObservationInvocation,
        *,
        cancellation_check: CancellationCheck | None = None,
        heartbeat: Heartbeat | None = None,
    ) -> ObservationRuntime:
        authority = invocation.runtime
        api = ApiClient(
            base_url=authority.riverhog_base_url,
            token=authority.capability_token,
            allow_insecure_http=authority.allow_insecure_http,
        )
        return cls(
            api,
            request=invocation.request,
            claim_id=invocation.claim_id,
            fence=invocation.fence,
            cancellation_check=cancellation_check,
            heartbeat=heartbeat,
            workspace_assurance=authority.workspace_assurance,
            owned_api=True,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def heartbeat(self) -> None:
        if self._closed:
            raise RuntimeError("observation runtime is closed")
        if self.cancellation_check is not None:
            self.cancellation_check()
        if self.external_heartbeat is not None:
            self.external_heartbeat()

    def refresh_capability(self, capability_token: str) -> None:
        self.heartbeat()
        current = self.api.current
        replacement = ApiClient(
            base_url=current.base_url,
            token=_token(capability_token),
            allow_insecure_http=current.allow_insecure_http,
        )
        replacement.host_header = current.host_header
        replacement.http2 = current.http2
        replacement.timeout_seconds = current.timeout_seconds
        replacement.upload_timeout_seconds = current.upload_timeout_seconds
        self.api.replace(replacement, owns_client=True)

    def subjects(self) -> tuple[tuple[ArtifactSubject, ClaimedArtifact], ...]:
        self.heartbeat()
        inventory = {artifact.key: artifact for artifact in self.reader.inventory()}
        resolved: list[tuple[ArtifactSubject, ClaimedArtifact]] = []
        for subject in self.request.subjects:
            key = (subject.collection.collection_id, subject.path)
            artifact = inventory.get(key)
            if (
                artifact is None
                or artifact.root != subject.collection.to_identity()
                or artifact.bytes != subject.bytes
                or artifact.sha256 != subject.sha256
            ):
                raise RuntimeError(
                    f"observer subject is not the current claimed artifact: {subject.id}"
                )
            resolved.append((subject, artifact))
        return tuple(resolved)

    def prepare(
        self,
        subjects: Sequence[ArtifactSubject] | None = None,
        **kwargs: Any,
    ) -> ClaimedRetrieval:
        available = dict(self.subjects())
        selected = tuple(subjects or self.request.subjects)
        if len({subject.id for subject in selected}) != len(selected):
            raise ValueError("observation subjects must be unique")
        artifacts: list[ClaimedArtifact] = []
        for subject in selected:
            artifact = available.get(subject)
            if artifact is None:
                raise ValueError(f"subject is not authorized by this observation: {subject.id}")
            artifacts.append(artifact)
        kwargs.setdefault("restore_policy", self.request.retrieval_policy)
        retrieval = self.reader.prepare(artifacts, **kwargs)
        self._retrievals.append(retrieval)
        return retrieval

    @contextmanager
    def stream(
        self,
        subject: ArtifactSubject,
        *,
        start: int = 0,
        end: int | None = None,
        chunk_size: int = 8 * 1024 * 1024,
        **prepare_kwargs: Any,
    ) -> Iterator[Iterator[bytes]]:
        with self.prepare((subject,), **prepare_kwargs) as retrieval:
            artifact = dict(self.subjects())[subject]
            with retrieval.stream(
                artifact,
                start=start,
                end=end,
                chunk_size=chunk_size,
            ) as chunks:
                yield chunks

    def read_bytes(
        self,
        subject: ArtifactSubject,
        *,
        maximum_bytes: int,
        **prepare_kwargs: Any,
    ) -> bytes:
        with self.prepare((subject,), **prepare_kwargs) as retrieval:
            artifact = dict(self.subjects())[subject]
            return retrieval.read_bytes(artifact, maximum_bytes=maximum_bytes)

    def open_workspace(self, root: Path) -> TransformWorkspace:
        """Open a request-bound encrypted or ephemeral observer workspace."""

        self.heartbeat()
        return TransformWorkspace.open(
            root,
            execution_id=self.request.request_id,
            assurance=self.workspace_assurance,
        )

    def materialize(
        self,
        subject: ArtifactSubject,
        *,
        workspace: TransformWorkspace,
        relative_path: str | None = None,
        **prepare_kwargs: Any,
    ) -> Path:
        output = workspace.resolve(relative_path or f"inputs/{subject.id}")
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.prepare((subject,), **prepare_kwargs) as retrieval:
            artifact = dict(self.subjects())[subject]
            retrieval.download(artifact, output)
        return output

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failures: list[Exception] = []
        for retrieval in self._retrievals:
            if retrieval.closed:
                continue
            try:
                retrieval.close(success=False)
            except Exception as exc:
                failures.append(exc)
        self.api.close()
        if failures:
            raise RuntimeError("failed to cancel active observation retrieval jobs") from failures[
                0
            ]


def _token(value: str) -> str:
    token = value.strip()
    if not token:
        raise ValueError("observer capability token must be nonempty")
    return token


__all__ = [
    "CancellationCheck",
    "ContentObserver",
    "Heartbeat",
    "ObservationRuntime",
]
