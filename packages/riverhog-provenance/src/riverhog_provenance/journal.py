from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import canonical_json, new_urn_uuid, utc_now
from .constants import PROVENANCE_ENTRY_SCHEMA, PROVENANCE_PROFILE
from .factory import get_observer
from .model import ObservationPolicy, ObservationRequest, PayloadBindingRequest
from .schema import validate_entry_document

RS = b"\x1e"
LF = b"\n"
JOURNAL_TYPE = "riverhog_provenance_journal_entry"
PRIMARY_PAYLOAD_ROLE = "co_resident_primary_payload"
SOFTWARE_AGENT_NAMESPACE = uuid.UUID("f5b76bbb-6a8b-4a25-9907-c3a8ae0a864c")

JsonObject = dict[str, Any]


class ProvenanceValidationError(ValueError):
    """A provenance journal or set does not satisfy the Riverhog v1 contract."""


@dataclass(frozen=True, slots=True)
class JournalFrame:
    sequence: int
    json_bytes: bytes
    document: JsonObject
    sha256: str


@dataclass(frozen=True, slots=True)
class ExternalStateReference:
    journal_id: str
    entry_id: str
    entry_json_sha256: str
    state_id: str


@dataclass(frozen=True, slots=True)
class JournalSummary:
    journal_id: str
    primary_lineage_id: str
    frames: tuple[JournalFrame, ...]
    journal_sha256: str
    current_binding_id: str
    current_state_id: str
    current_path: str
    current_bytes: int
    current_sha256: str
    agent_ids: frozenset[str]
    external_states: tuple[ExternalStateReference, ...]

    @property
    def tail(self) -> JournalFrame:
        return self.frames[-1]


def software_agent_id(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise ValueError("software agent name must not be empty")
    return f"urn:uuid:{uuid.uuid5(SOFTWARE_AGENT_NAMESPACE, normalized)}"


def software_agent(name: str, version: str) -> JsonObject:
    if not version.strip():
        raise ValueError("software agent version must not be empty")
    return {
        "id": software_agent_id(name),
        "type": "software",
        "name": name,
        "version": version,
        "vendor": "Riverhog",
    }


def encode_entry(document: Mapping[str, Any]) -> bytes:
    return RS + canonical_json(document) + LF


def journal_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def parse_journal(content: bytes) -> tuple[JournalFrame, ...]:
    if not content or content[:1] != RS:
        raise ProvenanceValidationError("journal must begin with an RFC 7464 record separator")
    chunks = content.split(RS)
    if chunks[0] != b"":
        raise ProvenanceValidationError("journal has bytes before its first record separator")
    frames: list[JournalFrame] = []
    for physical_sequence, chunk in enumerate(chunks[1:]):
        if not chunk.endswith(LF):
            raise ProvenanceValidationError(
                f"journal entry {physical_sequence} has no terminating LF"
            )
        json_bytes = chunk[:-1]
        if not json_bytes or json_bytes != json_bytes.strip():
            raise ProvenanceValidationError(
                f"journal entry {physical_sequence} has non-canonical outer whitespace"
            )
        try:
            document = json.loads(
                json_bytes,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ProvenanceValidationError(
                f"journal entry {physical_sequence} is not strict JSON: {exc}"
            ) from exc
        if not isinstance(document, dict):
            raise ProvenanceValidationError(
                f"journal entry {physical_sequence} must be a JSON object"
            )
        if canonical_json(document) != json_bytes:
            raise ProvenanceValidationError(
                f"journal entry {physical_sequence} is not canonical JSON"
            )
        frames.append(
            JournalFrame(
                sequence=physical_sequence,
                json_bytes=json_bytes,
                document=document,
                sha256=hashlib.sha256(json_bytes).hexdigest(),
            )
        )
    if not frames:
        raise ProvenanceValidationError("journal must contain at least one entry")
    return tuple(frames)


def validate_journal(content: bytes) -> JournalSummary:
    frames = parse_journal(content)
    journal_id = _required_string(frames[0].document, "journal_id")
    if frames[0].document.get("entry_kind") != "journal_init":
        raise ProvenanceValidationError("journal sequence zero must initialize the journal")

    states: dict[str, JsonObject] = {}
    agents: set[str] = set()
    active_bindings: dict[str, JsonObject] = {}
    external_states: dict[tuple[str, str, str, str], ExternalStateReference] = {}
    entry_by_id: dict[str, JournalFrame] = {}
    primary_lineage_id = ""

    for index, frame in enumerate(frames):
        document = frame.document
        try:
            validate_entry_document(document)
        except ValueError as exc:
            raise ProvenanceValidationError(
                f"journal entry {index} fails the Riverhog v1 schema: {exc}"
            ) from exc
        if document.get("$schema") != PROVENANCE_ENTRY_SCHEMA:
            raise ProvenanceValidationError(f"journal entry {index} uses another schema")
        if document.get("profile") != PROVENANCE_PROFILE:
            raise ProvenanceValidationError(f"journal entry {index} uses another profile")
        if document.get("type") != JOURNAL_TYPE:
            raise ProvenanceValidationError(f"journal entry {index} has another type")
        if document.get("sequence") != index:
            raise ProvenanceValidationError(
                f"journal entry {index} sequence does not match physical order"
            )
        if document.get("journal_id") != journal_id:
            raise ProvenanceValidationError(f"journal entry {index} changes journal identity")

        entry_id = _required_string(document, "id")
        if entry_id in entry_by_id:
            raise ProvenanceValidationError(f"journal repeats entry identity {entry_id}")
        entry_by_id[entry_id] = frame
        if index:
            previous = document.get("previous_entry")
            prior = frames[index - 1]
            if previous != {
                "entry_id": _required_string(prior.document, "id"),
                "sequence": index - 1,
                "json_sha256": prior.sha256,
            }:
                raise ProvenanceValidationError(
                    f"journal entry {index} does not commit to its exact predecessor"
                )

        assertions = _entry_assertions(document)
        if index == 0:
            body = document.get("body")
            journal = body.get("journal") if isinstance(body, dict) else None
            if not isinstance(journal, dict):
                raise ProvenanceValidationError("journal initialization has no policy")
            primary_lineage_id = _required_string(journal, "primary_lineage_id")

        for agent in _object_rows(assertions, "agents"):
            agents.add(_required_string(agent, "id"))
        for state in _object_rows(assertions, "states"):
            state_id = _required_string(state, "id")
            previous_state = states.get(state_id)
            if previous_state is not None and previous_state != state:
                raise ProvenanceValidationError(f"journal redefines state {state_id}")
            states[state_id] = state
        for binding in _object_rows(assertions, "payload_bindings"):
            role = _required_string(binding, "role")
            operation = binding.get("operation")
            if operation == "unbind":
                active_bindings.pop(role, None)
            elif operation == "bind":
                active_bindings[role] = binding
        for reference in _external_state_references(assertions):
            key = (
                reference.journal_id,
                reference.entry_id,
                reference.entry_json_sha256,
                reference.state_id,
            )
            external_states[key] = reference

    primary = active_bindings.get(PRIMARY_PAYLOAD_ROLE)
    if primary is None:
        raise ProvenanceValidationError("journal has no current primary payload binding")
    state_reference = primary.get("state")
    if not isinstance(state_reference, dict) or state_reference.get("scope") != "local":
        raise ProvenanceValidationError("current primary payload binding is not local")
    current_state_id = _required_string(state_reference, "id")
    current_state = states.get(current_state_id)
    if current_state is None:
        raise ProvenanceValidationError("current primary payload state is not asserted")
    if current_state.get("lineage_id") != primary_lineage_id:
        raise ProvenanceValidationError("current payload state is outside the primary lineage")
    locator = primary.get("relative_payload_locator")
    if not isinstance(locator, dict):
        raise ProvenanceValidationError("current payload binding has no relative locator")
    current_path = _required_string(locator, "text")
    current_bytes, current_sha256 = _state_content_identity(current_state)
    return JournalSummary(
        journal_id=journal_id,
        primary_lineage_id=primary_lineage_id,
        frames=frames,
        journal_sha256=journal_sha256(content),
        current_binding_id=_required_string(primary, "id"),
        current_state_id=current_state_id,
        current_path=current_path,
        current_bytes=current_bytes,
        current_sha256=current_sha256,
        agent_ids=frozenset(agents),
        external_states=tuple(external_states.values()),
    )


def validate_journal_set(journals: Mapping[str, bytes]) -> dict[str, JournalSummary]:
    summaries: dict[str, JournalSummary] = {}
    for declared_id, content in sorted(journals.items()):
        summary = validate_journal(content)
        if summary.journal_id != declared_id:
            raise ProvenanceValidationError(
                f"journal key {declared_id} does not match {summary.journal_id}"
            )
        summaries[declared_id] = summary
    for summary in summaries.values():
        for reference in summary.external_states:
            target = summaries.get(reference.journal_id)
            if target is None:
                raise ProvenanceValidationError(
                    f"journal {summary.journal_id} has an unresolved ancestor "
                    f"{reference.journal_id}"
                )
            frame = next(
                (item for item in target.frames if item.document.get("id") == reference.entry_id),
                None,
            )
            if frame is None or frame.sha256 != reference.entry_json_sha256:
                raise ProvenanceValidationError(
                    f"journal {summary.journal_id} has an invalid external entry commitment"
                )
            states = {
                _required_string(state, "id")
                for current in target.frames
                for state in _object_rows(_entry_assertions(current.document), "states")
            }
            if reference.state_id not in states:
                raise ProvenanceValidationError(
                    f"journal {summary.journal_id} references an absent external state"
                )
    return summaries


def verify_payload_binding(
    summary: JournalSummary,
    *,
    path: str,
    byte_count: int,
    sha256: str,
) -> None:
    if (
        summary.current_path != path
        or summary.current_bytes != byte_count
        or summary.current_sha256 != sha256
    ):
        raise ProvenanceValidationError(
            "current provenance state does not bind to the payload path, size, and SHA-256"
        )


def create_observation_journal(
    path: Path,
    *,
    relative_path: str,
    host_id: str,
    agent_name: str,
    agent_version: str,
    policy: ObservationPolicy | None = None,
) -> bytes:
    journal_id = new_urn_uuid()
    lineage_id = new_urn_uuid()
    agent = software_agent(agent_name, agent_version)
    init: JsonObject = {
        "$schema": PROVENANCE_ENTRY_SCHEMA,
        "profile": PROVENANCE_PROFILE,
        "schema_version": "1.0.0",
        "id": new_urn_uuid(),
        "type": JOURNAL_TYPE,
        "journal_id": journal_id,
        "sequence": 0,
        "recorded_at": utc_now(),
        "recorded_by_agent_id": agent["id"],
        "entry_kind": "journal_init",
        "body": {
            "journal": {
                "primary_lineage_id": lineage_id,
                "scope": "primary_lineage_with_related_provenance",
                "serialization": "rfc7464_json_text_sequence",
                "entry_digest_algorithm": "sha-256",
                "entry_digest_coverage": "json_text_octets_excluding_framing",
                "state_representation": "full_snapshot",
                "payload_semantics": "opaque_bytes",
                "correction_model": "monotonic_entry_supersession",
                "retention_intent": "permanent_archival",
                "label": relative_path,
            },
            "assertions": {
                "agents": [agent],
                "lineages": [
                    {
                        "id": lineage_id,
                        "type": "file_lineage",
                        "continuity_basis": "repository_tracking",
                        "asserted_by_agent_id": agent["id"],
                        "label": relative_path,
                    }
                ],
            },
        },
    }
    init_json = canonical_json(init)
    observation = get_observer().observe(
        ObservationRequest(
            path=path,
            lineage_id=lineage_id,
            host_id=host_id,
            observer_agent_id=str(agent["id"]),
            payload_binding=PayloadBindingRequest(relative_path=relative_path),
            policy=policy or ObservationPolicy(),
        )
    )
    assertion = observation.make_assertion_entry(
        journal_id=journal_id,
        sequence=1,
        previous_entry_id=str(init["id"]),
        previous_entry_json_sha256=hashlib.sha256(init_json).hexdigest(),
        recorded_by_agent_id=str(agent["id"]),
        omit_object_ids=(str(agent["id"]),),
    )
    content = encode_entry(init) + encode_entry(assertion)
    validate_journal(content)
    return content


def append_observation(
    content: bytes,
    path: Path,
    *,
    relative_path: str,
    host_id: str,
    agent_name: str,
    agent_version: str,
    policy: ObservationPolicy | None = None,
) -> bytes:
    summary = validate_journal(content)
    agent = software_agent(agent_name, agent_version)
    observation = get_observer().observe(
        ObservationRequest(
            path=path,
            lineage_id=summary.primary_lineage_id,
            host_id=host_id,
            observer_agent_id=str(agent["id"]),
            payload_binding=PayloadBindingRequest(
                relative_path=relative_path,
                replaces_binding_id=summary.current_binding_id,
            ),
            policy=policy or ObservationPolicy(),
        )
    )
    omit = (str(agent["id"]),) if agent["id"] in summary.agent_ids else ()
    assertions = observation.graph_fragment(omit_object_ids=omit)
    if agent["id"] not in summary.agent_ids:
        assertions["agents"] = [agent]
    entry = _assertion_entry(
        summary,
        assertions=assertions,
        recorded_by_agent_id=str(agent["id"]),
        recording_environment_id=str(observation.environment["id"]),
    )
    candidate = content + encode_entry(entry)
    validate_journal(candidate)
    return candidate


def append_replacement_transformation(
    content: bytes,
    output_path: Path,
    *,
    relative_path: str,
    host_id: str,
    agent_name: str,
    agent_version: str,
    event_label: str,
    started_at: str,
    ended_at: str,
    evidence: Sequence[Mapping[str, Any]] = (),
    policy: ObservationPolicy | None = None,
) -> bytes:
    summary = validate_journal(content)
    agent = software_agent(agent_name, agent_version)
    observation = get_observer().observe(
        ObservationRequest(
            path=output_path,
            lineage_id=summary.primary_lineage_id,
            host_id=host_id,
            observer_agent_id=str(agent["id"]),
            payload_binding=PayloadBindingRequest(
                relative_path=relative_path,
                replaces_binding_id=summary.current_binding_id,
            ),
            policy=policy or ObservationPolicy(),
        )
    )
    activity_id = new_urn_uuid()
    generated_state_id = str(observation.state["id"])
    assertions = observation.graph_fragment(omit_object_ids=(str(agent["id"]),))
    if agent["id"] not in summary.agent_ids:
        assertions["agents"] = [agent]
    activity: JsonObject = {
        "id": activity_id,
        "type": "file_state_transition",
        "event_type": "transformation",
        "event_label": event_label,
        "time": {"status": "exact", "started_at": started_at, "ended_at": ended_at},
        "environment_id": observation.environment["id"],
        "associations": [
            {
                "agent_id": agent["id"],
                "role": "executing_software",
                "plan_id": PROVENANCE_PROFILE,
            }
        ],
        "outcome": "success",
    }
    activity["evidence"] = (
        [dict(item) for item in evidence]
        if evidence
        else [
            {
                "id": new_urn_uuid(),
                "basis": "direct_process_record",
                "asserted_by_agent_id": agent["id"],
                "confidence": "high",
                "description": "Recorded by the software that performed the transformation.",
            }
        ]
    )
    assertions["activities"] = [activity]
    assertions["relations"] = [
        {
            "id": new_urn_uuid(),
            "type": "usage",
            "activity_id": activity_id,
            "state": {"id": summary.current_state_id, "scope": "local"},
            "role": "source",
        },
        {
            "id": new_urn_uuid(),
            "type": "generation",
            "activity_id": activity_id,
            "state": {"id": generated_state_id, "scope": "local"},
            "role": "replacement",
        },
        {
            "id": new_urn_uuid(),
            "type": "derivation",
            "activity_id": activity_id,
            "used_state": {"id": summary.current_state_id, "scope": "local"},
            "generated_state": {"id": generated_state_id, "scope": "local"},
            "derivation_kind": "transformation",
        },
    ]
    entry = _assertion_entry(
        summary,
        assertions=assertions,
        recorded_by_agent_id=str(agent["id"]),
        recording_environment_id=str(observation.environment["id"]),
    )
    candidate = content + encode_entry(entry)
    validate_journal(candidate)
    return candidate


def current_state_reference(content: bytes) -> ExternalStateReference:
    """Commit to the exact entry that asserts a journal's current state."""

    summary = validate_journal(content)
    for frame in reversed(summary.frames):
        if any(
            state.get("id") == summary.current_state_id
            for state in _object_rows(_entry_assertions(frame.document), "states")
        ):
            return ExternalStateReference(
                journal_id=summary.journal_id,
                entry_id=_required_string(frame.document, "id"),
                entry_json_sha256=frame.sha256,
                state_id=summary.current_state_id,
            )
    raise ProvenanceValidationError("current state has no asserting journal entry")


def create_derivative_journal(
    output_path: Path,
    *,
    relative_path: str,
    source_journals: Sequence[bytes],
    host_id: str,
    agent_name: str,
    agent_version: str,
    event_label: str,
    started_at: str,
    ended_at: str,
    derivation_kind: str = "transformation",
    evidence: Sequence[Mapping[str, Any]] = (),
    policy: ObservationPolicy | None = None,
) -> bytes:
    """Create a new lineage that commits to every contributing source state."""

    allowed_kinds = {
        "revision",
        "transformation",
        "copy",
        "metadata_change",
        "relocation",
        "aggregation",
        "extraction",
    }
    if derivation_kind not in allowed_kinds:
        raise ProvenanceValidationError("unsupported derivative kind")
    if not source_journals:
        raise ProvenanceValidationError("a derivative requires at least one source journal")

    references = [current_state_reference(content) for content in source_journals]
    if len({(item.journal_id, item.state_id) for item in references}) != len(references):
        raise ProvenanceValidationError("a derivative source state must not be repeated")

    content = create_observation_journal(
        output_path,
        relative_path=relative_path,
        host_id=host_id,
        agent_name=agent_name,
        agent_version=agent_version,
        policy=policy,
    )
    summary = validate_journal(content)
    agent = software_agent(agent_name, agent_version)
    state_frame = next(
        frame
        for frame in reversed(summary.frames)
        if any(
            state.get("id") == summary.current_state_id
            for state in _object_rows(_entry_assertions(frame.document), "states")
        )
    )
    environment_id = _required_string(state_frame.document, "recording_environment_id")
    activity_id = new_urn_uuid()
    activity: JsonObject = {
        "id": activity_id,
        "type": "file_state_transition",
        "event_type": "transformation",
        "event_label": event_label,
        "time": {"status": "exact", "started_at": started_at, "ended_at": ended_at},
        "environment_id": environment_id,
        "associations": [
            {
                "agent_id": agent["id"],
                "role": "executing_software",
                "plan_id": PROVENANCE_PROFILE,
            }
        ],
        "outcome": "success",
        "evidence": (
            [dict(item) for item in evidence]
            if evidence
            else [
                {
                    "id": new_urn_uuid(),
                    "basis": "direct_process_record",
                    "asserted_by_agent_id": agent["id"],
                    "confidence": "high",
                    "description": "Recorded by the software that produced the derivative.",
                }
            ]
        ),
    }
    local_state = {"id": summary.current_state_id, "scope": "local"}
    relations: list[JsonObject] = [
        {
            "id": new_urn_uuid(),
            "type": "generation",
            "activity_id": activity_id,
            "state": local_state,
            "role": "derivative",
        }
    ]
    for reference in references:
        external_state: JsonObject = {
            "id": reference.state_id,
            "scope": "external",
            "journal_id": reference.journal_id,
            "entry_id": reference.entry_id,
            "entry_json_sha256": reference.entry_json_sha256,
        }
        relations.extend(
            [
                {
                    "id": new_urn_uuid(),
                    "type": "usage",
                    "activity_id": activity_id,
                    "state": external_state,
                    "role": "source",
                },
                {
                    "id": new_urn_uuid(),
                    "type": "derivation",
                    "activity_id": activity_id,
                    "used_state": external_state,
                    "generated_state": local_state,
                    "derivation_kind": derivation_kind,
                },
            ]
        )
    entry = _assertion_entry(
        summary,
        assertions={"activities": [activity], "relations": relations},
        recorded_by_agent_id=str(agent["id"]),
        recording_environment_id=environment_id,
    )
    candidate = content + encode_entry(entry)
    validate_journal(candidate)
    return candidate


def _assertion_entry(
    summary: JournalSummary,
    *,
    assertions: Mapping[str, Any],
    recorded_by_agent_id: str,
    recording_environment_id: str,
) -> JsonObject:
    return {
        "$schema": PROVENANCE_ENTRY_SCHEMA,
        "profile": PROVENANCE_PROFILE,
        "schema_version": "1.0.0",
        "id": new_urn_uuid(),
        "type": JOURNAL_TYPE,
        "journal_id": summary.journal_id,
        "sequence": len(summary.frames),
        "recorded_at": utc_now(),
        "recorded_by_agent_id": recorded_by_agent_id,
        "recording_environment_id": recording_environment_id,
        "entry_kind": "assertion",
        "previous_entry": {
            "entry_id": _required_string(summary.tail.document, "id"),
            "sequence": summary.tail.sequence,
            "json_sha256": summary.tail.sha256,
        },
        "body": {"assertions": dict(assertions)},
    }


def _unique_object(pairs: Iterable[tuple[str, Any]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value}")


def _required_string(value: Mapping[str, Any], key: str) -> str:
    current = value.get(key)
    if not isinstance(current, str) or not current:
        raise ProvenanceValidationError(f"required string {key!r} is absent")
    return current


def _entry_assertions(document: Mapping[str, Any]) -> Mapping[str, Any]:
    body = document.get("body")
    if not isinstance(body, dict):
        return {}
    if document.get("entry_kind") == "correction":
        replacement = body.get("replacement")
        return replacement if isinstance(replacement, dict) else {}
    assertions = body.get("assertions")
    return assertions if isinstance(assertions, dict) else {}


def _object_rows(assertions: Mapping[str, Any], key: str) -> tuple[JsonObject, ...]:
    rows = assertions.get(key)
    if not isinstance(rows, list):
        return ()
    return tuple(item for item in rows if isinstance(item, dict))


def _state_content_identity(state: Mapping[str, Any]) -> tuple[int, str]:
    content = state.get("content")
    if not isinstance(content, dict):
        raise ProvenanceValidationError("file state has no content identity")
    byte_count = content.get("size_bytes")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
        raise ProvenanceValidationError("file state has an invalid byte count")
    digests = content.get("digests")
    if not isinstance(digests, list):
        raise ProvenanceValidationError("file state has no digest list")
    sha256 = next(
        (
            item.get("value")
            for item in digests
            if isinstance(item, dict)
            and item.get("algorithm") == "sha-256"
            and item.get("encoding") == "hex"
            and item.get("purpose") == "fixity"
        ),
        None,
    )
    if not isinstance(sha256, str) or len(sha256) != 64:
        raise ProvenanceValidationError("file state has no SHA-256 fixity digest")
    return byte_count, sha256


def _walk_json(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _external_state_references(
    assertions: Mapping[str, Any],
) -> tuple[ExternalStateReference, ...]:
    references: list[ExternalStateReference] = []
    for item in _walk_json(assertions):
        if item.get("scope") != "external":
            continue
        try:
            references.append(
                ExternalStateReference(
                    journal_id=_required_string(item, "journal_id"),
                    entry_id=_required_string(item, "entry_id"),
                    entry_json_sha256=_required_string(item, "entry_json_sha256"),
                    state_id=_required_string(item, "id"),
                )
            )
        except ProvenanceValidationError:
            continue
    return tuple(references)


__all__ = [
    "ExternalStateReference",
    "JournalFrame",
    "JournalSummary",
    "ProvenanceValidationError",
    "append_observation",
    "append_replacement_transformation",
    "create_observation_journal",
    "encode_entry",
    "journal_sha256",
    "parse_journal",
    "software_agent",
    "software_agent_id",
    "validate_journal",
    "validate_journal_set",
    "verify_payload_binding",
]
