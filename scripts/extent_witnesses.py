"""Nonnormative executable witnesses for segmented v1 extent decisions."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REQUIRED_CLAIMS = frozenset(
    {
        "bounded_step",
        "forward_progress",
        "multiple_segments",
        "no_silent_truncation",
        "restart",
    }
)


@dataclass(frozen=True, slots=True)
class BehavioralWitness:
    """One reviewed group of executable proofs for an owned progression rule."""

    id: str
    owner: str
    reasons: tuple[str, ...]
    test_node_ids: tuple[str, ...]
    gates: tuple[str, ...]
    claims: tuple[str, ...] = tuple(sorted(REQUIRED_CLAIMS))


WITNESSES = (
    BehavioralWitness(
        id="riverhog-upload-work-progression/v1",
        owner="riverhog",
        reasons=("bounded-actionable-work-acquisition",),
        test_node_ids=(
            "packages/riverhog-protocol/tests/test_collection_upload_transport.py::"
            "test_bounded_upload_work_batch_binds_assignment_and_checkpoint_state",
            "tests/unit/test_archive_write_reaper.py::"
            "test_archive_maintenance_drains_bounded_progress_before_idle_interval",
            "tests/unit/test_incremental_collection_producer.py::"
            "test_many_artifact_publication_retains_only_the_unsealed_pack_window",
        ),
        gates=("make unit", "make compose-smoke"),
    ),
    BehavioralWitness(
        id="riverhog-archive-volume-part-progression/v1",
        owner=(
            "https://nashspence.github.io/riverhog/v1/schemas/"
            "collection-archive-volume-v1.schema.json"
        ),
        reasons=("bounded-archive-volume-parts",),
        test_node_ids=(
            "tests/unit/test_incremental_plan.py::"
            "test_artifact_at_a_time_construction_seals_the_exact_one_shot_v1_plans",
            "tests/unit/test_pack_upload.py::"
            "test_checkpoint_resumes_across_riverhog_and_adapter_restart",
            "tests/unit/test_raw_upload.py::"
            "test_raw_upload_resumes_on_server_defined_age_part_boundaries",
        ),
        gates=("make unit", "make compose-smoke", "make provider-qualification"),
    ),
    BehavioralWitness(
        id="riverhog-work-authority-append/v1",
        owner="riverhog",
        reasons=("bounded-authority-append",),
        test_node_ids=(
            "companions/stove0/tests/test_riverhog_adapter.py::"
            "test_post_root_settlement_restarts_from_bounded_portable_inventory_progress",
            "tests/integration/test_collection_deletion_concurrency.py::"
            "test_postgres_exact_output_intent_creation_resumes_one_upload",
        ),
        gates=("make unit", "make postgres-concurrency", "make compose-smoke"),
    ),
    BehavioralWitness(
        id="riverhog-work-disposition-append/v1",
        owner="riverhog",
        reasons=("bounded-disposition-append",),
        test_node_ids=(
            "companions/stove0/tests/test_riverhog_adapter.py::"
            "test_post_root_settlement_restarts_from_bounded_portable_inventory_progress",
            "tests/integration/test_collection_deletion_concurrency.py::"
            "test_postgres_concurrent_first_disposition_and_output_create_one_set",
        ),
        gates=("make unit", "make postgres-concurrency", "make compose-smoke"),
    ),
    BehavioralWitness(
        id="riverhog-provenance-volume-progression/v1",
        owner=(
            "https://nashspence.github.io/riverhog/v1/schemas/"
            "riverhog-provenance-bindings-v1.schema.json"
        ),
        reasons=("bounded-provenance-binding-volume",),
        test_node_ids=(
            "packages/riverhog-provenance/tests/test_segmented_archive.py::"
            "test_ordered_segmented_provenance_authority_round_trips",
            "packages/riverhog-provenance/tests/test_segmented_archive.py::"
            "test_provenance_segmentation_limits_one_volume_not_the_logical_total",
            "tests/unit/test_collection_uploads.py::"
            "test_provenance_append_persists_next_ordinal_across_retry_and_restart",
        ),
        gates=("make unit", "make compose-smoke", "make provider-qualification"),
    ),
    BehavioralWitness(
        id="riverhog-raw-digest-progression/v1",
        owner="riverhog",
        reasons=("bounded-raw-digest-append",),
        test_node_ids=(
            "tests/unit/test_raw_ingress_manifest.py::"
            "test_raw_source_is_hashed_once_into_small_authority_and_bounded_batches",
            "tests/unit/test_raw_upload.py::"
            "test_raw_upload_resumes_on_server_defined_age_part_boundaries",
        ),
        gates=("make unit", "make compose-smoke", "make provider-qualification"),
    ),
    BehavioralWitness(
        id="riverhog-retrieval-work-progression/v1",
        owner="riverhog",
        reasons=("bounded-retrieval-work-request",),
        test_node_ids=(
            "tests/unit/test_retrieval_service.py::"
            "test_retrieval_plan_resumes_across_more_than_two_internal_segment_pages",
            "tests/integration/test_catalog_schema_postgres.py::"
            "test_postgres_retrieval_plan_advances_in_bounded_restartable_steps",
        ),
        gates=("make unit", "make postgres-concurrency", "make compose-smoke"),
    ),
    BehavioralWitness(
        id="riverhog-read-collection-progression/v1",
        owner="riverhog",
        reasons=("bounded-route-page", "bounded-route-progression"),
        test_node_ids=(
            "tests/unit/test_public_interface_parity.py::"
            "test_public_read_collection_selectors_are_bounded_and_frozen",
            "tests/unit/test_catalog_sync.py::"
            "test_catalog_sync_crosses_many_pages_and_repairs_fixed_frontier_changes",
            "tests/integration/test_lifecycle_event_concurrency.py::"
            "test_event_reads_and_concurrent_context_reapers_do_only_bounded_work",
        ),
        gates=("make unit", "make postgres-concurrency", "make database-qualification"),
    ),
    BehavioralWitness(
        id="stove0-read-collection-progression/v1",
        owner="stove0",
        reasons=("bounded-route-page", "bounded-route-progression"),
        test_node_ids=(
            "tests/unit/test_public_interface_parity.py::"
            "test_public_read_collection_selectors_are_bounded_and_frozen",
            "companions/stove0/tests/test_cli.py::"
            "test_stove0_bounded_pages_keep_rich_and_json_cli_parity",
            "companions/stove0/tests/test_work_state.py::"
            "test_sql_operational_retention_scans_bounded_pages_without_parsing_all_work",
        ),
        gates=("make unit", "make database-qualification", "make compose-smoke"),
    ),
    BehavioralWitness(
        id="riverhog-upload-registration-progression/v1",
        owner="riverhog",
        reasons=("bounded-upload-registration",),
        test_node_ids=(
            "packages/riverhog-transform-sdk/tests/test_collection_transform_sdk.py::"
            "test_producer_streams_bounded_batches_without_limiting_collection_size",
            "tests/integration/test_collection_upload_custody_concurrency.py::"
            "test_distinct_concurrent_registrations_preserve_both_members",
        ),
        gates=("make unit", "make postgres-concurrency", "make compose-smoke"),
    ),
    BehavioralWitness(
        id="riverhog-upload-tag-staging-progression/v1",
        owner="riverhog",
        reasons=("bounded-upload-staging-step; collection-tag-set-is-unbounded",),
        test_node_ids=(
            "tests/unit/test_incremental_collection_producer.py::"
            "test_incremental_producer_stages_unbounded_logical_tags_in_bounded_requests",
            "packages/riverhog-protocol/tests/test_collection_tag_protocol.py::"
            "test_late_tag_page_seeks_by_fixed_identity_without_rescanning_prior_tags",
            "tests/unit/test_collection_tags.py::"
            "test_provider_nodes_for_retained_exact_revisions_remain_recoverable",
            "tests/unit/test_collection_tags.py::"
            "test_tag_history_cleanup_bounds_all_subordinate_rows_and_restarts",
            "tests/integration/test_catalog_schema_postgres.py::"
            "test_postgres_tag_history_cleanup_serializes_its_row_work_budget",
        ),
        gates=("make unit", "make compose-smoke", "make provider-qualification"),
    ),
    BehavioralWitness(
        id="riverhog-upload-unit-source-progression/v1",
        owner="riverhog",
        reasons=("bounded-upload-unit-source-map",),
        test_node_ids=(
            "tests/unit/test_incremental_collection_producer.py::"
            "test_many_artifact_publication_retains_only_the_unsealed_pack_window",
            "tests/unit/test_pack_volume.py::"
            "test_pack_unit_wire_payload_is_only_concatenated_source_bytes",
            "tests/unit/test_pack_upload.py::"
            "test_checkpoint_resumes_across_riverhog_and_adapter_restart",
        ),
        gates=("make unit", "make compose-smoke", "make provider-qualification"),
    ),
)


class ExtentWitnessError(RuntimeError):
    """Raised when the nonnormative proof bindings no longer resolve."""


def _test_symbol_exists(root: Path, node_id: str) -> bool:
    path_value, separator, symbol = node_id.partition("::")
    if not separator or not symbol or "::" in symbol:
        return False
    path = root / path_value
    if not path.is_file():
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol
        for node in tree.body
    )


def bind_segmented_decisions(
    root: Path,
    decisions: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Bind every segmented decision to current reviewed executable proof sources."""

    identities = [witness.id for witness in WITNESSES]
    if len(identities) != len(set(identities)):
        raise ExtentWitnessError("behavioral witness identities are not unique")
    for witness in WITNESSES:
        if set(witness.claims) != REQUIRED_CLAIMS:
            raise ExtentWitnessError(f"behavioral witness has incomplete claims: {witness.id}")
        if not witness.test_node_ids or not witness.gates:
            raise ExtentWitnessError(f"behavioral witness is not executable: {witness.id}")
        missing = [
            node_id for node_id in witness.test_node_ids if not _test_symbol_exists(root, node_id)
        ]
        if missing:
            raise ExtentWitnessError(
                f"behavioral witness has stale test nodes: {witness.id}: {missing}"
            )

    used: set[str] = set()
    links: list[dict[str, object]] = []
    for decision in decisions:
        if decision.get("policy") != "segmented_no_total_max":
            continue
        owner = str(decision.get("owner"))
        reason = str(decision.get("reason"))
        matched = [
            witness.id
            for witness in WITNESSES
            if witness.owner == owner and reason in witness.reasons
        ]
        if not matched:
            raise ExtentWitnessError(
                "segmented extent has no executable behavioral witness: "
                f"{decision.get('id')} ({owner}; {reason})"
            )
        used.update(matched)
        links.append(
            {
                "id": decision["id"],
                "behavioral_witnesses": matched,
            }
        )
    unused = sorted(set(identities) - used)
    if unused:
        raise ExtentWitnessError(f"behavioral witnesses have no segmented owner: {unused}")
    records: list[dict[str, object]] = [
        {
            "id": witness.id,
            "owner": witness.owner,
            "reasons": list(witness.reasons),
            "test_node_ids": list(witness.test_node_ids),
            "gates": list(witness.gates),
            "claims": list(witness.claims),
        }
        for witness in WITNESSES
        if witness.id in used
    ]
    return records, links


__all__ = [
    "BehavioralWitness",
    "ExtentWitnessError",
    "REQUIRED_CLAIMS",
    "WITNESSES",
    "bind_segmented_decisions",
]
