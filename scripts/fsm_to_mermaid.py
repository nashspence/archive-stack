from __future__ import annotations

import argparse
import html
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "contracts" / "operator" / "statecharts.yaml"
sys.path.insert(0, str(ROOT))

from contracts.operator import copy as operator_copy  # noqa: E402


class StatechartContractError(ValueError):
    pass


class OperatorCopyReferenceError(ValueError):
    pass


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StatechartContractError(f"{label} must be a mapping")
    return value


def load_contract(
    path: Path = DEFAULT_CONTRACT,
) -> tuple[Mapping[str, Mapping[str, Any]], Sequence[Mapping[str, Any]]]:
    contract = _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), label=str(path))
    if contract.get("version") != 1:
        raise StatechartContractError("statechart contract version must be 1")
    statecharts = _mapping(contract.get("statecharts"), label="statecharts")
    handoffs = contract.get("handoffs", [])
    if not isinstance(handoffs, Sequence) or isinstance(handoffs, str):
        raise StatechartContractError("handoffs must be a list")
    return {
        str(name): _mapping(statechart, label=f"statecharts.{name}")
        for name, statechart in statecharts.items()
    }, [_mapping(handoff, label="handoffs[]") for handoff in handoffs]


def load_statecharts(path: Path = DEFAULT_CONTRACT) -> Mapping[str, Mapping[str, Any]]:
    statecharts, _handoffs = load_contract(path)
    return statecharts


def _state_label(state_name: str) -> str:
    return state_name.replace("_", " ").title()


def _mermaid_text(value: object) -> str:
    return html.escape(str(value), quote=False).replace('"', "'")


def _display_text(value: str) -> str:
    return "<br/>".join(_mermaid_text(line) for line in value.splitlines())


def _node_label(title: str, body: str | None = None) -> str:
    label = f"<b>{_mermaid_text(title)}</b>"
    if body:
        return f"{label}<br/>{body}"
    return label


def _state_display_label(
    state_name: str,
    state: Mapping[str, Any],
    *,
    include_copy: bool = True,
) -> str:
    title = _state_label(state_name)
    view = state.get("view")
    if not view or not include_copy:
        return _node_label(title)
    return _node_label(title, _display_text(render_operator_copy(str(view))))


def _transition_kind_and_value(
    name: str,
    state_name: str,
    transition: Mapping[str, Any],
) -> tuple[str, str] | None:
    keys = [key for key in ("event", "guard") if transition.get(key)]
    if not keys:
        return None
    if len(keys) > 1:
        raise StatechartContractError(
            f"{name}.{state_name} transition may define only one of event or guard"
        )
    key = keys[0]
    return key, str(transition[key])


def _transition_display_label(value: str) -> str:
    words = re.sub(r"[_\-.]+", " ", value).strip()
    return words.title() if words else value


def _transition_state_id(
    *,
    value: str,
    source: str,
    target: str,
    index: int,
) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value.replace("-", "_").replace(".", "_"))
    normalized = normalized.strip("_").lower() or "transition"
    if normalized[0].isdigit():
        normalized = f"transition_{normalized}"
    return f"{normalized}_{source}_{target}_{index}"


def _node_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value.replace("-", "_").replace(".", "_"))
    normalized = normalized.strip("_").lower() or "node"
    if normalized[0].isdigit():
        normalized = f"node_{normalized}"
    return normalized


def _state_node_line(state_name: str, label: str) -> str:
    return f'    {state_name}["{label}"]:::stateNode'


def _handoff_endpoint(handoff: Mapping[str, Any], key: str) -> tuple[str, str]:
    endpoint = _mapping(handoff.get(key), label=f"handoffs[].{key}")
    return str(endpoint.get("statechart", "")), str(endpoint.get("state", ""))


def _handoff_label(handoff: Mapping[str, Any]) -> str:
    label = handoff.get("label")
    if not label:
        from_statechart, _from_state = _handoff_endpoint(handoff, "from")
        to_statechart, _to_state = _handoff_endpoint(handoff, "target")
        label = f"{from_statechart} to {to_statechart}"
    return _transition_display_label(str(label))


def _link_node_id(*, source_state: str, target_statechart: str, target_state: str) -> str:
    return (
        f"link_{_node_id(source_state)}_to_"
        f"{_node_id(target_statechart)}_{_node_id(target_state)}"
    )


def _external_node_id(*, statechart_name: str, state_name: str) -> str:
    return f"external_{_node_id(statechart_name)}_{_node_id(state_name)}"


def _link_node_line(link_id: str, label: str) -> str:
    return f'    {link_id}[["{_node_label(label)}"]]:::linkNode'


def _external_state_node_line(
    *,
    node_id: str,
    statechart_name: str,
    state_name: str,
) -> str:
    return (
        f'    {node_id}["'
        f'{_node_label(_transition_display_label(statechart_name), _state_label(state_name))}'
        '"]:::externalStateNode'
    )


def _transition_node_line(transition_id: str, label: str, kind: str) -> str:
    rendered = _node_label(label)
    match kind:
        case "event":
            return f'    {transition_id}(["{rendered}"]):::eventNode'
        case "guard":
            return f'    {transition_id}{{"{rendered}"}}:::guardNode'
    raise StatechartContractError(f"unsupported transition type: {kind}")


def _guided_item_text(item: operator_copy.GuidedItem) -> str:
    return "\n".join(
        (
            operator_copy.guided_item_header(index=1, total=1, item=item),
            operator_copy.guided_item_body(item=item),
        )
    )


def _notification_text(notification: operator_copy.ActionNeededNotification) -> str:
    return "\n".join(
        (
            notification.title,
            notification.body,
        )
    )


def render_operator_copy(reference: str) -> str:
    match reference:
        case "arc_home_no_attention":
            return operator_copy.arc_home_no_attention()
        case "arc_home_attention":
            return operator_copy.arc_home_attention(
                [
                    operator_copy.arc_item_cloud_backup_failed(
                        collection_id="docs",
                        attempts=2,
                        latest_error=None,
                    )
                ]
            )
        case "arc_home_at_will_menu":
            return operator_copy.arc_home_at_will_menu()
        case "arc_item_notification_health_failed":
            return _guided_item_text(
                operator_copy.arc_item_notification_health_failed(
                    channel="Push",
                    latest_error="delivery timeout",
                )
            )
        case "arc_item_setup_needs_attention":
            return _guided_item_text(
                operator_copy.arc_item_setup_needs_attention(
                    area="Storage",
                    summary="missing bucket",
                )
            )
        case "arc_item_billing_needs_attention":
            return _guided_item_text(
                operator_copy.arc_item_billing_needs_attention(
                    summary="pricing unavailable",
                )
            )
        case "arc_item_cloud_backup_failed":
            return _guided_item_text(
                operator_copy.arc_item_cloud_backup_failed(
                    collection_id="docs",
                    attempts=2,
                    latest_error=None,
                )
            )
        case "arc_item_upload_retry_available":
            return _guided_item_text(
                operator_copy.arc_item_upload_retry_available(collection_id="docs")
            )
        case "upload_prompt_collection_id":
            return operator_copy.upload_prompt_collection_id()
        case "upload_prompt_source_path":
            return operator_copy.upload_prompt_source_path()
        case "upload_started":
            return operator_copy.upload_started(
                collection_id="photos-2024",
                files=2,
                total_bytes=2048,
            )
        case "upload_progress":
            return operator_copy.upload_progress(
                collection_id="photos-2024",
                uploaded_files=1,
                total_files=2,
                uploaded_bytes=1024,
                total_bytes=2048,
            )
        case "upload_archiving":
            return operator_copy.upload_archiving(collection_id="photos-2024")
        case "upload_finalized":
            return operator_copy.upload_finalized(
                collection_id="photos-2024",
                files=2,
                total_bytes=2048,
            )
        case "upload_failed_cloud_backup":
            return operator_copy.upload_failed_cloud_backup(
                collection_id="docs",
                attempts=2,
                latest_error="s3 timeout",
            )
        case "upload_canceled":
            return operator_copy.upload_canceled(collection_id="photos-2024")
        case "hot_search_header":
            return operator_copy.hot_search_header(query="invoice", result_count=2)
        case "hot_search_no_results":
            return operator_copy.hot_search_no_results(query="invoice")
        case "hot_file_available":
            return operator_copy.hot_file_available(
                path="docs/tax/2022/invoice-123.pdf",
                size=1024,
            )
        case "hot_file_archived_only":
            return operator_copy.hot_file_archived_only(
                path="docs/tax/2022/invoice-123.pdf",
            )
        case "get_starting":
            return operator_copy.get_starting(
                target="docs/tax/2022/invoice-123.pdf",
                output_path="./invoice-123.pdf",
            )
        case "get_written":
            return operator_copy.get_written(
                path="docs/tax/2022/invoice-123.pdf",
                output_path="./invoice-123.pdf",
                bytes_written=1024,
            )
        case "get_not_hot":
            return operator_copy.get_not_hot(target="docs/tax/2022/invoice-123.pdf")
        case "pin_ready":
            return operator_copy.pin_ready(target="docs/tax/2022/invoice-123.pdf")
        case "pin_waiting_for_disc":
            return operator_copy.pin_waiting_for_disc(
                target="docs/tax/2022/invoice-123.pdf",
                missing_bytes=None,
            )
        case "pins_list_header":
            return operator_copy.pins_list_header(pin_count=2)
        case "fetch_detail_pending":
            return operator_copy.fetch_detail_pending(
                target="docs/tax/2022/invoice-123.pdf",
                pending_files=1,
                partial_files=1,
            )
        case "release_done":
            return operator_copy.release_done(target="docs/tax/2022/invoice-123.pdf")
        case "collection_summary":
            return operator_copy.collection_summary(
                collection_id="docs",
                cloud_backup_safe=True,
                disc_coverage="partial",
                labels=["20260420T040001Z-1"],
                storage_locations=["Shelf B1"],
            )
        case "collection_fully_protected":
            return operator_copy.collection_fully_protected(collection_id="docs")
        case "collection_needs_attention":
            return operator_copy.collection_needs_attention(
                collection_id="docs",
                reason="cloud backup needs attention",
            )
        case "plan_disc_work_ready":
            return operator_copy.plan_disc_work_ready(
                collection_ids=["docs"],
                disc_count=1,
            )
        case "plan_no_disc_work":
            return operator_copy.plan_no_disc_work()
        case "images_physical_work_summary":
            return operator_copy.images_physical_work_summary(
                discs_needed=1,
                fully_protected_collections=1,
            )
        case "cloud_backup_report":
            return operator_copy.cloud_backup_report(
                collection_id="docs",
                estimated_monthly_cost="0.01",
                healthy=True,
            )
        case "cloud_backup_billing_detail_unavailable":
            return operator_copy.cloud_backup_billing_detail_unavailable(
                reason="pricing unavailable"
            )
        case "copy_registered":
            return operator_copy.copy_registered(
                label_text="20260420T040001Z-1",
                location="Shelf B1",
            )
        case "copy_list_item":
            return operator_copy.copy_list_item(
                label_text="20260420T040001Z-1",
                location="Shelf B1",
                state="verified",
            )
        case "copy_moved":
            return operator_copy.copy_moved(
                label_text="20260420T040001Z-1",
                location="Shelf B2",
            )
        case "copy_marked_verified":
            return operator_copy.copy_marked_verified(label_text="20260420T040001Z-1")
        case "copy_marked_lost":
            return operator_copy.copy_marked_lost(label_text="20260420T040001Z-1")
        case "copy_marked_damaged":
            return operator_copy.copy_marked_damaged(label_text="20260420T040001Z-1")
        case "doctor_ok":
            return operator_copy.doctor_ok()
        case "doctor_needs_attention":
            return operator_copy.doctor_needs_attention(["Storage bucket missing"])
        case "billing_unavailable":
            return operator_copy.billing_unavailable(reason="pricing unavailable")
        case "notification_health_failed":
            return operator_copy.notification_health_failed(
                channel="Push",
                latest_error="delivery timeout",
            )
        case "arc_disc_no_attention":
            return operator_copy.arc_disc_no_attention()
        case "arc_disc_attention":
            return operator_copy.arc_disc_attention(
                [operator_copy.disc_item_burn_work_ready(disc_count=1)]
            )
        case "disc_item_unfinished_local_copy":
            return _guided_item_text(
                operator_copy.disc_item_unfinished_local_copy(
                    label_text="20260420T040001Z-1"
                )
            )
        case "disc_item_recovery_ready":
            return _guided_item_text(
                operator_copy.disc_item_recovery_ready(
                    session_id="rs-20260420T040001Z-rebuild-1",
                    affected=["docs"],
                    expires_at="2026-05-02 08:00 UTC",
                )
            )
        case "disc_item_recovery_approval_required":
            return _guided_item_text(
                operator_copy.disc_item_recovery_approval_required(
                    session_id="rs-20260420T040001Z-rebuild-1",
                    affected=["docs"],
                    estimated_cost="12.34",
                )
            )
        case "disc_item_hot_recovery_needs_media":
            return _guided_item_text(
                operator_copy.disc_item_hot_recovery_needs_media(
                    target="docs/tax/2022/invoice-123.pdf"
                )
            )
        case "disc_item_replacement_disc_needed":
            return _guided_item_text(
                operator_copy.disc_item_replacement_disc_needed(
                    label_text="20260420T040001Z-1"
                )
            )
        case "disc_item_burn_work_ready":
            return _guided_item_text(operator_copy.disc_item_burn_work_ready(disc_count=1))
        case "disc_item_recovery_expired":
            return _guided_item_text(
                operator_copy.disc_item_recovery_expired(
                    session_id="rs-20260420T040001Z-rebuild-1"
                )
            )
        case "burn_no_work":
            return operator_copy.burn_no_work()
        case "burn_ready":
            return operator_copy.burn_ready(disc_count=1, estimated_bytes=2048)
        case "burn_insert_blank_disc":
            return operator_copy.burn_insert_blank_disc(
                label_text="20260420T040001Z-1",
                device="/dev/sr0",
            )
        case "burn_verifying_prepared_disc":
            return operator_copy.burn_verifying_prepared_disc(
                label_text="20260420T040001Z-1"
            )
        case "burn_writing_disc":
            return operator_copy.burn_writing_disc(
                label_text="20260420T040001Z-1",
                device="/dev/sr0",
            )
        case "burn_verifying_disc":
            return operator_copy.burn_verifying_disc(label_text="20260420T040001Z-1")
        case "burn_label_checkpoint":
            return operator_copy.burn_label_checkpoint(label_text="20260420T040001Z-1")
        case "burn_location_prompt":
            return operator_copy.burn_location_prompt(label_text="20260420T040001Z-1")
        case "burn_registered":
            return operator_copy.burn_registered(
                label_text="20260420T040001Z-1",
                location="Shelf B1",
            )
        case "burn_resume_unlabeled_copy":
            return operator_copy.burn_resume_unlabeled_copy(
                label_text="20260420T040001Z-1"
            )
        case "burn_unlabeled_copy_unavailable":
            return operator_copy.burn_unlabeled_copy_unavailable(
                label_text="20260420T040001Z-1"
            )
        case "burn_backlog_cleared":
            return operator_copy.burn_backlog_cleared()
        case "recovery_approval_required":
            return operator_copy.recovery_approval_required(
                session_id="rs-20260420T040001Z-rebuild-1",
                affected=["docs"],
                estimated_cost="12.34",
            )
        case "recovery_requested":
            return operator_copy.recovery_requested(
                session_id="rs-20260420T040001Z-rebuild-1"
            )
        case "recovery_waiting":
            return operator_copy.recovery_waiting(
                session_id="rs-20260420T040001Z-rebuild-1",
                expected_ready_at="2026-05-02 08:00 UTC",
            )
        case "recovery_ready":
            return operator_copy.recovery_ready(
                session_id="rs-20260420T040001Z-rebuild-1",
                affected=["docs"],
                expires_at="2026-05-02 08:00 UTC",
            )
        case "recovery_completed":
            return operator_copy.recovery_completed(
                session_id="rs-20260420T040001Z-rebuild-1"
            )
        case "recovery_expired_local_resume":
            return operator_copy.recovery_expired_local_resume(
                session_id="rs-20260420T040001Z-rebuild-1"
            )
        case "recovery_expired_needs_reapproval":
            return operator_copy.recovery_expired_needs_reapproval(
                session_id="rs-20260420T040001Z-rebuild-1",
                affected=["docs"],
                estimated_cost="12.34",
            )
        case "recovery_cleanup_handoff":
            return operator_copy.recovery_cleanup_handoff(affected=["docs"])
        case "hot_recovery_insert_disc":
            return operator_copy.hot_recovery_insert_disc(
                target="docs/tax/2022/invoice-123.pdf",
                disc_label="20260420T040001Z-1",
            )
        case "hot_recovery_progress":
            return operator_copy.hot_recovery_progress(
                target="docs/tax/2022/invoice-123.pdf",
                restored_bytes=1024,
                total_bytes=2048,
            )
        case "hot_recovery_retry_other_disc":
            return operator_copy.hot_recovery_retry_other_disc(
                target="docs/tax/2022/invoice-123.pdf"
            )
        case "hot_recovery_done":
            return operator_copy.hot_recovery_done(
                target="docs/tax/2022/invoice-123.pdf"
            )
        case "push_burn_work_ready":
            return _notification_text(
                operator_copy.push_burn_work_ready(
                    disc_count=1,
                    oldest_ready_at="2026-05-01 08:00 UTC",
                )
            )
        case "push_disc_work_waiting_too_long":
            return _notification_text(
                operator_copy.push_disc_work_waiting_too_long(
                    disc_count=1,
                    oldest_ready_at="2026-05-01 08:00 UTC",
                )
            )
        case "push_replacement_disc_needed":
            return _notification_text(
                operator_copy.push_replacement_disc_needed(
                    label_text="20260420T040001Z-1"
                )
            )
        case "push_recovery_approval_required":
            return _notification_text(
                operator_copy.push_recovery_approval_required(
                    affected=["docs"],
                    estimated_cost="12.34",
                )
            )
        case "push_recovery_ready":
            return _notification_text(
                operator_copy.push_recovery_ready(
                    affected=["docs"],
                    expires_at="2026-05-02 08:00 UTC",
                )
            )
        case "push_hot_recovery_needs_media":
            return _notification_text(
                operator_copy.push_hot_recovery_needs_media(
                    target="docs/tax/2022/invoice-123.pdf"
                )
            )
        case "push_cloud_backup_failed":
            return _notification_text(
                operator_copy.push_cloud_backup_failed(collection_id="docs", attempts=2)
            )
        case "push_notification_health_failed":
            return _notification_text(
                operator_copy.push_notification_health_failed(channel="Push")
            )
        case "push_billing_needs_attention":
            return _notification_text(
                operator_copy.push_billing_needs_attention(reason="pricing unavailable")
            )
        case "push_setup_needs_attention":
            return _notification_text(
                operator_copy.push_setup_needs_attention(
                    area="Storage",
                    summary="missing bucket",
                )
            )
    raise OperatorCopyReferenceError(f"unsupported operator copy reference: {reference}")


def _class_def_lines(initial_state: str | None = None) -> list[str]:
    lines = [
        "    classDef stateNode fill:#f8fafc,stroke:#334155,color:#0f172a,"
        "stroke-width:1px;",
        "    classDef initialState stroke:#111827,stroke-width:3px;",
        "    classDef eventNode fill:#e0f2fe,stroke:#0369a1,color:#0f172a,"
        "stroke-width:2px;",
        "    classDef guardNode fill:#fef3c7,stroke:#b45309,color:#0f172a,"
        "stroke-width:2px;",
        "    classDef linkNode fill:#f3e8ff,stroke:#7e22ce,color:#0f172a,"
        "stroke-width:2px;",
        "    classDef externalStateNode fill:#faf5ff,stroke:#7e22ce,color:#0f172a,"
        "stroke-width:1px;",
    ]
    if initial_state:
        lines.append(f"    class {initial_state} initialState;")
    return lines


def _state_nodes_for(
    *,
    statechart_name: str,
    states: Mapping[str, Any],
    include_copy: bool,
) -> list[str]:
    lines: list[str] = []
    for state_name, state in states.items():
        _mapping(state, label=f"{statechart_name}.states.{state_name}")
        label = _state_display_label(
            str(state_name),
            _mapping(state, label=str(state_name)),
            include_copy=include_copy,
        )
        lines.append(_state_node_line(str(state_name), label))
    return lines


def _transition_lines_for(
    *,
    statechart_name: str,
    states: Mapping[str, Any],
) -> tuple[list[str], list[tuple[str, str, str]]]:
    lines: list[str] = []
    transition_nodes: list[tuple[str, str, str]] = []
    for state_name, raw_state in states.items():
        state = _mapping(raw_state, label=f"{statechart_name}.states.{state_name}")
        raw_transitions = state.get("transitions", [])
        if not isinstance(raw_transitions, Sequence) or isinstance(raw_transitions, str):
            raise StatechartContractError(
                f"{statechart_name}.{state_name}.transitions must be a list"
            )
        for index, raw_transition in enumerate(raw_transitions, start=1):
            transition = _mapping(
                raw_transition,
                label=f"{statechart_name}.states.{state_name}.transitions[]",
            )
            target = str(transition.get("target", ""))
            source_id = str(state_name)
            target_id = target
            kind_and_value = _transition_kind_and_value(
                statechart_name,
                str(state_name),
                transition,
            )
            if kind_and_value is None:
                lines.append(f"    {source_id} --> {target_id}")
                continue
            kind, value = kind_and_value
            transition_id = _transition_state_id(
                value=value,
                source=source_id,
                target=target_id,
                index=index,
            )
            transition_nodes.append((transition_id, _transition_display_label(value), kind))
            lines.append(f"    {source_id} --> {transition_id}")
            lines.append(f"    {transition_id} --> {target_id}")
    return lines, transition_nodes


def _handoff_lines_for(
    *,
    statechart_name: str,
    states: Mapping[str, Any],
    statecharts: Mapping[str, Mapping[str, Any]],
    handoffs: Sequence[Mapping[str, Any]],
) -> list[str]:
    lines: list[str] = []
    handoff_nodes: list[tuple[str, str]] = []
    external_nodes: dict[tuple[str, str], str] = {}

    for handoff in handoffs:
        from_statechart, from_state = _handoff_endpoint(handoff, "from")
        if from_statechart != statechart_name:
            continue
        to_statechart, to_state = _handoff_endpoint(handoff, "target")
        if from_state not in states:
            raise StatechartContractError(
                f"handoff source state does not exist: {from_statechart}.{from_state}"
            )
        target_statechart = _mapping(
            statecharts.get(to_statechart),
            label=f"statecharts.{to_statechart}",
        )
        target_states = _mapping(
            target_statechart.get("states"),
            label=f"{to_statechart}.states",
        )
        if to_state not in target_states:
            raise StatechartContractError(
                f"handoff target state does not exist: {to_statechart}.{to_state}"
            )

        link_id = _link_node_id(
            source_state=from_state,
            target_statechart=to_statechart,
            target_state=to_state,
        )
        external_id = external_nodes.setdefault(
            (to_statechart, to_state),
            _external_node_id(statechart_name=to_statechart, state_name=to_state),
        )
        lines.append(f"    {from_state} --> {link_id}")
        lines.append(f"    {link_id} --> {external_id}")
        handoff_nodes.append((link_id, _handoff_label(handoff)))

    if handoff_nodes:
        lines.append("")
        for link_id, label in handoff_nodes:
            lines.append(_link_node_line(link_id, label))
        for (to_statechart, to_state), external_id in external_nodes.items():
            lines.append(
                _external_state_node_line(
                    node_id=external_id,
                    statechart_name=to_statechart,
                    state_name=to_state,
                )
            )
    return lines


def render_statechart(
    name: str,
    statechart: Mapping[str, Any],
    *,
    statecharts: Mapping[str, Mapping[str, Any]] | None = None,
    handoffs: Sequence[Mapping[str, Any]] = (),
) -> str:
    states = _mapping(statechart.get("states"), label=f"{name}.states")
    initial = str(statechart.get("initial", ""))
    if initial not in states:
        raise StatechartContractError(f"{name} initial state {initial!r} does not exist")

    lines = [
        "%% Generated from contracts/operator/statecharts.yaml",
        f"%% statechart: {name}",
        f"%% initial: {initial}",
        "flowchart TD",
        "",
    ]
    command = statechart.get("command")
    if command:
        lines.insert(3, f"%% command: {command}")

    lines.extend(
        _state_nodes_for(
            statechart_name=name,
            states=states,
            include_copy=True,
        )
    )
    lines.append("")

    transition_lines, transition_nodes = _transition_lines_for(
        statechart_name=name,
        states=states,
    )
    lines.extend(transition_lines)

    if transition_nodes:
        lines.append("")
        for transition_id, label, _kind in transition_nodes:
            lines.append(_transition_node_line(transition_id, label, _kind))
        lines.append("")

    if statecharts is not None:
        handoff_lines = _handoff_lines_for(
            statechart_name=name,
            states=states,
            statecharts=statecharts,
            handoffs=handoffs,
        )
        if handoff_lines:
            lines.append("")
            lines.extend(handoff_lines)

    lines.append("")
    lines.extend(_class_def_lines(initial))

    return "\n".join(lines).rstrip() + "\n"


def _selected_statecharts(
    statecharts: Mapping[str, Mapping[str, Any]],
    names: Sequence[str],
) -> list[tuple[str, Mapping[str, Any]]]:
    if not names:
        return list(statecharts.items())

    missing = [name for name in names if name not in statecharts]
    if missing:
        available = ", ".join(statecharts)
        raise StatechartContractError(
            f"unknown statechart {', '.join(missing)}; available: {available}"
        )
    return [(name, statecharts[name]) for name in names]


def _filename_for_statechart(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name) + ".mmd"


def _write_outputs(
    selected: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    out_dir: Path,
    statecharts: Mapping[str, Mapping[str, Any]],
    handoffs: Sequence[Mapping[str, Any]],
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name, statechart in selected:
        path = out_dir / _filename_for_statechart(name)
        path.write_text(
            render_statechart(
                name,
                statechart,
                statecharts=statecharts,
                handoffs=handoffs,
            ),
            encoding="utf-8",
        )
        paths.append(path)
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render operator statechart contracts as Mermaid workflow diagrams.",
    )
    parser.add_argument(
        "statecharts",
        nargs="*",
        help="optional statechart ids to render; defaults to every statechart",
    )
    parser.add_argument(
        "--contract",
        type=Path,
        default=DEFAULT_CONTRACT,
        help="statechart YAML contract path",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        help="write one .mmd file per selected statechart instead of printing",
    )
    args = parser.parse_args(argv)

    try:
        statecharts, handoffs = load_contract(args.contract)
        selected = _selected_statecharts(statecharts, args.statecharts)
        if args.out_dir:
            for path in _write_outputs(
                selected,
                out_dir=args.out_dir,
                statecharts=statecharts,
                handoffs=handoffs,
            ):
                print(path)
            return 0
        rendered = [
            render_statechart(
                name,
                statechart,
                statecharts=statecharts,
                handoffs=handoffs,
            ).rstrip()
            for name, statechart in selected
        ]
        print("\n".join(rendered))
    except (OperatorCopyReferenceError, StatechartContractError) as exc:
        print(f"fsm_to_mermaid: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
