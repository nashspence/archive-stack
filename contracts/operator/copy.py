from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from .format import (
    bytes_amount,
    command,
    count_noun,
    deadline,
    list_sentence,
    money_usd,
    truncate,
    when,
)

OperatorCommand = Literal["arc", "arc-disc"]
Urgency = Literal["attention", "important", "time-sensitive", "approval"]

ARC: OperatorCommand = "arc"
ARC_DISC: OperatorCommand = "arc-disc"

OPERATOR_TERMS: tuple[str, ...] = (
    "collection",
    "files",
    "hot storage",
    "disc",
    "blank disc",
    "replacement disc",
    "label",
    "storage location",
    "cloud backup",
    "recovery",
    "safe",
    "needs attention",
    "fully protected",
)

MACHINE_ONLY_TERMS: tuple[str, ...] = (
    "candidate",
    "copy slot",
    "fetch manifest",
    "finalized image",
    "Glacier",
    "glacier_path",
    "image_rebuild",
    "pending_approval",
    "protection_state",
    "ready_to_finalize",
    "recovery-byte stream",
    "waiting_for_future_iso",
)


@dataclass(frozen=True, slots=True)
class OperatorAction:
    command: OperatorCommand
    label: str | None = None

    def __post_init__(self) -> None:
        expected = f"Run {self.command}"
        if self.label is None:
            object.__setattr__(self, "label", expected)
            return
        if self.label != expected:
            raise ValueError(f"operator action label must be {expected!r}")

    @property
    def argv(self) -> tuple[str, ...]:
        return (self.command,)

    def payload(self) -> dict[str, object]:
        return {
            "label": self.label,
            "command": self.command,
            "argv": list(self.argv),
        }


@dataclass(frozen=True, slots=True)
class ActionNeededNotification:
    event: str
    title: str
    body: str
    action: OperatorAction
    urgency: Urgency = "attention"
    reminder_event: str | None = None
    reminder_title: str | None = None
    reminder_body: str | None = None
    reminder_after: timedelta | None = None

    def payload(
        self,
        *,
        reminder: bool = False,
        reminder_count: int | None = None,
        delivered_at: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "event": self.reminder_event if reminder and self.reminder_event else self.event,
            "title": self.reminder_title if reminder and self.reminder_title else self.title,
            "body": self.reminder_body if reminder and self.reminder_body else self.body,
            "urgency": self.urgency,
            "actions": [self.action.payload()],
        }
        if reminder_count is not None:
            payload["reminder_count"] = reminder_count
        if delivered_at is not None:
            payload["delivered_at"] = delivered_at
        return payload


@dataclass(frozen=True, slots=True)
class GuidedItem:
    kind: str
    priority: int
    command: OperatorCommand
    title: str
    body: str
    next_step: str

    @property
    def action(self) -> OperatorAction:
        return OperatorAction(self.command)


def _run_arc() -> OperatorAction:
    return OperatorAction(ARC)


def _run_arc_disc() -> OperatorAction:
    return OperatorAction(ARC_DISC)


def _error_detail(latest_error: str | None, *, prefix: str = "Last error") -> str:
    if not latest_error:
        return ""
    return f" {prefix}: {truncate(latest_error, max_chars=120)}"


# Shared guided flow copy


def guided_intro(*, cli_name: OperatorCommand, item_count: int) -> str:
    if item_count == 0:
        return f"{cli_name} has no items that need attention right now."
    return f"{cli_name} found {count_noun(item_count, 'item')} that need attention."


def guided_item_header(*, index: int, total: int, item: GuidedItem) -> str:
    return f"Step {index} of {total}: {item.title}"


def guided_item_body(*, item: GuidedItem) -> str:
    return f"{item.body}\nNext step: {item.next_step}"


def guided_all_done(*, cli_name: OperatorCommand) -> str:
    return f"{cli_name} has cleared the current items that need attention."


def guided_stopped(*, cli_name: OperatorCommand) -> str:
    return (
        f"{cli_name} stopped before all items were cleared. "
        f"Run {command(cli_name)} again to continue."
    )


# no-arg arc / operator home


def arc_home_no_attention() -> str:
    return (
        "No attention needed.\n"
        "You can upload a collection, search hot storage, pin files, get files, "
        "or release pins when you choose."
    )


def arc_home_attention(items: Sequence[GuidedItem]) -> str:
    lines = [guided_intro(cli_name=ARC, item_count=len(items))]
    for index, item in enumerate(items, start=1):
        lines.extend(("", guided_item_header(index=index, total=len(items), item=item)))
        lines.append(guided_item_body(item=item))
    lines.extend(("", f"Run {command(ARC)} to walk these in priority order."))
    return "\n".join(lines)


def arc_home_at_will_menu() -> str:
    return (
        "At-will workflows:\n"
        "1. Upload a collection\n"
        "2. Search hot storage\n"
        "3. Pin files into hot storage\n"
        "4. Get a hot file\n"
        "5. Release pins"
    )


def arc_item_notification_health_failed(
    *,
    channel: str,
    latest_error: str | None = None,
) -> GuidedItem:
    return GuidedItem(
        kind="notification_health_failed",
        priority=10,
        command=ARC,
        title="Notifications need attention",
        body=(
            f"{channel} notifications may not be working."
            f"{_error_detail(latest_error)} "
            "Fix this so Riverhog can tell you when recovery or disc work needs attention."
        ),
        next_step="Check notification delivery.",
    )


def arc_item_setup_needs_attention(*, area: str, summary: str) -> GuidedItem:
    return GuidedItem(
        kind="setup_needs_attention",
        priority=20,
        command=ARC,
        title="Setup needs attention",
        body=f"{area}: {truncate(summary, max_chars=140)}",
        next_step="Run the guided setup check.",
    )


def arc_item_billing_needs_attention(*, summary: str) -> GuidedItem:
    return GuidedItem(
        kind="billing_needs_attention",
        priority=30,
        command=ARC,
        title="Recovery cost checks need attention",
        body=truncate(summary, max_chars=160),
        next_step="Check recovery cost information before approving recovery work.",
    )


def arc_item_cloud_backup_failed(
    *,
    collection_id: str,
    attempts: int,
    latest_error: str | None = None,
) -> GuidedItem:
    return GuidedItem(
        kind="cloud_backup_failed",
        priority=40,
        command=ARC,
        title="Cloud backup needs attention",
        body=(
            f"Collection {collection_id} did not finish cloud backup after "
            f"{count_noun(attempts, 'try', 'tries')}. It is not fully protected yet."
            f"{_error_detail(latest_error)}"
        ),
        next_step="Review the failed cloud backup and retry cloud backup safely.",
    )


def arc_item_upload_retry_available(*, collection_id: str) -> GuidedItem:
    return GuidedItem(
        kind="collection_upload_retry",
        priority=50,
        command=ARC,
        title="Collection upload can be retried",
        body=(
            f"Collection {collection_id} is not visible yet because its previous "
            "upload or cloud backup did not complete."
        ),
        next_step="Resume or retry the collection upload.",
    )


# no-arg arc-disc / physical and recovery backlog


def arc_disc_no_attention() -> str:
    return (
        "arc-disc has no disc or recovery work right now.\n"
        "No blank disc, replacement disc, or recovery approval is needed."
    )


def arc_disc_attention(items: Sequence[GuidedItem]) -> str:
    lines = [guided_intro(cli_name=ARC_DISC, item_count=len(items))]
    for index, item in enumerate(items, start=1):
        lines.extend(("", guided_item_header(index=index, total=len(items), item=item)))
        lines.append(guided_item_body(item=item))
    lines.extend(("", f"Run {command(ARC_DISC)} to clear this backlog in the safest order."))
    return "\n".join(lines)


def disc_item_unfinished_local_copy(*, label_text: str) -> GuidedItem:
    return GuidedItem(
        kind="unfinished_local_disc",
        priority=10,
        command=ARC_DISC,
        title="Finish the disc already in progress",
        body=(
            f"Riverhog has a verified disc waiting for label {label_text}. "
            "Finish labeling and record the storage location before starting new disc work."
        ),
        next_step="Resume the unfinished disc work.",
    )


def disc_item_recovery_ready(
    *,
    session_id: str,
    affected: Iterable[str],
    expires_at: datetime | str | None,
) -> GuidedItem:
    return GuidedItem(
        kind="recovery_ready",
        priority=20,
        command=ARC_DISC,
        title="Recovery is ready",
        body=(
            f"Recovery {session_id} is ready for {list_sentence(affected)} "
            f"{deadline(expires_at)}. Finish this before the ready window closes."
        ),
        next_step="Finish the guided recovery.",
    )


def disc_item_recovery_approval_required(
    *,
    session_id: str,
    affected: Iterable[str],
    estimated_cost: object | None,
) -> GuidedItem:
    return GuidedItem(
        kind="recovery_approval_required",
        priority=30,
        command=ARC_DISC,
        title="Recovery needs approval",
        body=(
            f"Recovery {session_id} can restore {list_sentence(affected)}. "
            f"Estimated cost: {money_usd(estimated_cost)}. "
            "Approve only if you want Riverhog to request the cloud backup files."
        ),
        next_step="Review and approve or leave recovery paused.",
    )


def disc_item_hot_recovery_needs_media(*, target: str) -> GuidedItem:
    return GuidedItem(
        kind="hot_recovery_needs_media",
        priority=40,
        command=ARC_DISC,
        title="A disc is needed for hot storage",
        body=(
            f"Files need recovery from disc for {truncate(target)}. "
            "Riverhog needs a disc to restore missing files to hot storage."
        ),
        next_step="Insert the requested disc and restore the files.",
    )


def disc_item_replacement_disc_needed(*, label_text: str | None = None) -> GuidedItem:
    subject = f"label {label_text}" if label_text else "a protected copy"
    return GuidedItem(
        kind="replacement_disc_needed",
        priority=50,
        command=ARC_DISC,
        title="Replacement disc needed",
        body=(
            f"Riverhog needs a replacement disc for {subject}. "
            "The guided flow will choose what to write."
        ),
        next_step="Make the replacement disc.",
    )


def disc_item_burn_work_ready(
    *,
    disc_count: int,
    oldest_ready_at: datetime | str | None = None,
) -> GuidedItem:
    waiting = ""
    if oldest_ready_at is not None:
        waiting = f" The oldest has been waiting since {when(oldest_ready_at)}."
    return GuidedItem(
        kind="burn_work_ready",
        priority=60,
        command=ARC_DISC,
        title="Blank discs are needed",
        body=f"Riverhog has {count_noun(disc_count, 'disc')} ready to write.{waiting}",
        next_step="Start the guided disc session.",
    )


def disc_item_recovery_expired(*, session_id: str) -> GuidedItem:
    return GuidedItem(
        kind="recovery_expired",
        priority=70,
        command=ARC_DISC,
        title="Recovery window expired",
        body=(
            f"Recovery {session_id} is no longer ready. "
            "Riverhog needs to choose the next safe recovery step."
        ),
        next_step="Review the recovery.",
    )


# collection upload


def upload_prompt_collection_id() -> str:
    return "What should this collection be called?"


def upload_prompt_source_path() -> str:
    return "Where are the files for this collection?"


def upload_started(*, collection_id: str, files: int, total_bytes: int | None) -> str:
    return (
        f"Started upload for collection {collection_id}.\n"
        f"Riverhog will add {count_noun(files, 'file')} totaling "
        f"{bytes_amount(total_bytes)}. The collection becomes visible only after "
        "upload, verification, and cloud backup are complete."
    )


def upload_progress(
    *,
    collection_id: str,
    uploaded_files: int,
    total_files: int,
    uploaded_bytes: int | None,
    total_bytes: int | None,
) -> str:
    return (
        f"Uploading collection {collection_id}: {uploaded_files} of "
        f"{count_noun(total_files, 'file')} and {bytes_amount(uploaded_bytes)} "
        f"of {bytes_amount(total_bytes)}."
    )


def upload_archiving(*, collection_id: str) -> str:
    return (
        f"Collection {collection_id} uploaded successfully.\n"
        "Riverhog is finishing cloud backup now. The collection is not marked safe "
        "until that backup is verified."
    )


def upload_finalized(*, collection_id: str, files: int, total_bytes: int | None) -> str:
    return (
        f"Collection {collection_id} is safe.\n"
        f"Cloud backup is safe. {count_noun(files, 'file')} totaling "
        f"{bytes_amount(total_bytes)} are uploaded, verified, and ready for disc planning."
    )


def upload_failed_cloud_backup(
    *,
    collection_id: str,
    attempts: int,
    latest_error: str | None,
) -> str:
    return (
        f"Collection {collection_id} uploaded, but cloud backup did not finish "
        f"after {count_noun(attempts, 'try', 'tries')}.\n"
        f"It is not fully protected yet. Run {command(ARC)} for the guided next step."
        f"{_error_detail(latest_error)}"
    )


def upload_canceled(*, collection_id: str) -> str:
    return (
        f"Upload for collection {collection_id} was canceled. "
        "The collection is not visible yet."
    )


# hot storage and at-will software workflows


def hot_search_header(*, query: str, result_count: int) -> str:
    return (
        f"Found {count_noun(result_count, 'result')} for {truncate(query)} "
        "in hot storage."
    )


def hot_search_no_results(*, query: str) -> str:
    return f"No hot storage results found for {truncate(query)}."


def hot_file_available(*, path: str, size: int | None) -> str:
    return f"{truncate(path)} is available in hot storage ({bytes_amount(size)})."


def hot_file_archived_only(*, path: str) -> str:
    return (
        f"{truncate(path)} is safe in the archive, but not currently in hot storage. "
        "Pin it if you want Riverhog to bring it back."
    )


def get_starting(*, target: str, output_path: str) -> str:
    return f"Getting {truncate(target)} from hot storage into {output_path}."


def get_written(*, path: str, output_path: str, bytes_written: int | None) -> str:
    return f"Wrote {truncate(path)} to {output_path} ({bytes_amount(bytes_written)})."


def get_not_hot(*, target: str) -> str:
    return (
        f"{truncate(target)} is not in hot storage right now. "
        f"Pin it first, then run {command(ARC_DISC)} if Riverhog needs a disc."
    )


def pin_ready(*, target: str) -> str:
    return f"{truncate(target)} is pinned and available in hot storage."


def pin_waiting_for_disc(*, target: str, missing_bytes: int | None) -> str:
    return (
        f"Files need recovery from disc. {truncate(target)} is pinned, "
        "but Riverhog needs a disc to restore "
        f"{bytes_amount(missing_bytes)} to hot storage.\n"
        f"Run {command(ARC_DISC)} for the guided disc workflow."
    )


def pins_list_header(*, pin_count: int) -> str:
    return f"{count_noun(pin_count, 'pin')} currently keeping files in hot storage."


def fetch_detail_pending(*, target: str, pending_files: int, partial_files: int) -> str:
    return (
        f"Files need recovery from disc for {truncate(target)}.\n"
        f"Pending files: {pending_files}. Partly restored files: {partial_files}."
    )


def release_done(*, target: str) -> str:
    return (
        f"{truncate(target)} is no longer pinned. "
        "Riverhog may free hot-storage space when it is safe."
    )


# collection, planning, cloud backup, and physical coverage detail commands


def collection_summary(
    *,
    collection_id: str,
    cloud_backup_safe: bool,
    disc_coverage: str,
    labels: Iterable[str] = (),
    storage_locations: Iterable[str] = (),
) -> str:
    cloud = "cloud backup is safe" if cloud_backup_safe else "cloud backup needs attention"
    return (
        f"Collection {collection_id}: {cloud}.\n"
        f"Disc coverage is {disc_coverage}.\n"
        f"Labels: {list_sentence(labels)}.\n"
        f"Storage locations: {list_sentence(storage_locations)}."
    )


def collection_fully_protected(*, collection_id: str) -> str:
    return f"Collection {collection_id} is fully protected."


def collection_needs_attention(*, collection_id: str, reason: str) -> str:
    return f"Collection {collection_id} needs attention: {truncate(reason, max_chars=160)}."


def plan_disc_work_ready(*, collection_ids: Iterable[str], disc_count: int) -> str:
    return (
        f"Disc work is ready for {list_sentence(collection_ids)}. "
        f"Have {count_noun(disc_count, 'blank disc')} available."
    )


def plan_no_disc_work() -> str:
    return "No disc work is ready right now."


def images_physical_work_summary(
    *,
    discs_needed: int,
    fully_protected_collections: int,
) -> str:
    return (
        f"Disc work needs attention: {count_noun(discs_needed, 'disc')} needed. "
        f"{count_noun(fully_protected_collections, 'collection')} fully protected. "
        f"Run {command(ARC_DISC)}."
    )


def cloud_backup_report(
    *,
    collection_id: str | None,
    estimated_monthly_cost: object | None,
    healthy: bool,
) -> str:
    subject = f" for collection {collection_id}" if collection_id else ""
    state = "healthy" if healthy else "needs attention"
    return (
        f"Cloud backup{subject} is {state}. "
        f"Estimated monthly cost: {money_usd(estimated_monthly_cost)}."
    )


def cloud_backup_billing_detail_unavailable(*, reason: str | None = None) -> str:
    detail = f" Reason: {truncate(reason, max_chars=120)}." if reason else ""
    return f"Cloud backup cost details are not available right now.{detail}"


# physical copy facts


def copy_registered(*, label_text: str, location: str) -> str:
    return f"Disc label {label_text} is recorded at storage location {location}."


def copy_list_item(*, label_text: str, location: str | None, state: str) -> str:
    stored = location or "storage location not recorded"
    return f"Disc label {label_text}: {state}, {stored}."


def copy_moved(*, label_text: str, location: str) -> str:
    return f"Disc label {label_text} is now recorded at storage location {location}."


def copy_marked_verified(*, label_text: str) -> str:
    return f"Disc label {label_text} is verified."


def copy_marked_lost(*, label_text: str) -> str:
    return (
        f"Disc label {label_text} is marked lost. "
        "Riverhog will tell you if a replacement disc or recovery is needed."
    )


def copy_marked_damaged(*, label_text: str) -> str:
    return (
        f"Disc label {label_text} is marked damaged. "
        "Riverhog will tell you if a replacement disc or recovery is needed."
    )


# guided burn and replacement disc sessions


def burn_no_work() -> str:
    return "No disc work is waiting. Riverhog does not need a blank disc right now."


def burn_ready(*, disc_count: int, estimated_bytes: int | None = None) -> str:
    size = f" totaling {bytes_amount(estimated_bytes)}" if estimated_bytes is not None else ""
    return f"Riverhog has {count_noun(disc_count, 'disc')}{size} ready to write."


def burn_insert_blank_disc(*, label_text: str, device: str | None = None) -> str:
    location = f" into {device}" if device else ""
    return (
        f"Insert a blank disc for label {label_text}{location}, then press Enter.\n"
        "Riverhog will write and verify it before asking you to label it."
    )


def burn_verifying_prepared_disc(*, label_text: str) -> str:
    return f"Checking prepared contents for disc label {label_text} before writing."


def burn_writing_disc(*, label_text: str, device: str | None = None) -> str:
    location = f" to {device}" if device else ""
    return f"Writing disc label {label_text}{location}. Keep the disc available."


def burn_verifying_disc(*, label_text: str) -> str:
    return (
        f"Verifying disc label {label_text}. "
        "Riverhog will not count it until verification passes."
    )


def burn_label_checkpoint(*, label_text: str) -> str:
    return (
        "Write this exact label on the disc:\n"
        f"{label_text}\n\n"
        "After the disc is labeled, type labeled to continue. "
        f"Riverhog will not count label {label_text} as protected until this is confirmed."
    )


def burn_location_prompt(*, label_text: str) -> str:
    return f"Where will disc label {label_text} be stored? Enter the storage location."


def burn_registered(*, label_text: str, location: str) -> str:
    return f"Disc label {label_text} is verified, labeled, and recorded at {location}."


def burn_resume_unlabeled_copy(*, label_text: str) -> str:
    return (
        f"Riverhog previously wrote and verified disc label {label_text}, "
        "but it was not confirmed as labeled. If that disc is still available, "
        "Riverhog can resume at labeling."
    )


def burn_unlabeled_copy_unavailable(*, label_text: str) -> str:
    return (
        f"Disc label {label_text} is not available. "
        "Riverhog will write a new disc instead."
    )


def burn_backlog_cleared() -> str:
    return "Disc work complete. The disc backlog is clear."


# recovery sessions


def recovery_approval_required(
    *,
    session_id: str,
    affected: Iterable[str],
    estimated_cost: object | None,
    warnings: Iterable[str] = (),
) -> str:
    warning_text = ""
    if warnings:
        warning_text = f"\nWarnings: {list_sentence(warnings, max_items=4)}"
    return (
        f"Recovery {session_id} needs approval.\n"
        f"Affected: {list_sentence(affected)}\n"
        f"Estimated cost: {money_usd(estimated_cost)}\n"
        "Approve only when you want Riverhog to request the cloud backup files."
        f"{warning_text}"
    )


def recovery_requested(*, session_id: str) -> str:
    return (
        f"Recovery {session_id} is approved.\n"
        "Riverhog has requested the cloud backup files and will notify you when they are ready."
    )


def recovery_waiting(*, session_id: str, expected_ready_at: datetime | str | None) -> str:
    return f"Recovery {session_id} is waiting. Expected ready time: {when(expected_ready_at)}."


def recovery_ready(
    *,
    session_id: str,
    affected: Iterable[str],
    expires_at: datetime | str | None,
) -> str:
    return (
        f"Recovery {session_id} is ready.\n"
        f"Recovered data for {list_sentence(affected)} is available {deadline(expires_at)}."
    )


def recovery_completed(*, session_id: str) -> str:
    return f"Recovery {session_id} is complete."


def recovery_expired(*, session_id: str) -> str:
    return (
        f"Recovery {session_id} is no longer ready. "
        f"Run {command(ARC_DISC)} to review the next safe recovery step."
    )


def recovery_cleanup_handoff(*, affected: Iterable[str]) -> str:
    return (
        f"Recovery cleanup is ready for {list_sentence(affected)}. "
        "Riverhog will keep the safe recovery handoff."
    )


# hot-storage recovery from disc


def hot_recovery_insert_disc(*, target: str, disc_label: str | None) -> str:
    disc = f"disc label {disc_label}" if disc_label else "the needed disc"
    return f"Insert {disc} to restore {truncate(target)} to hot storage."


def hot_recovery_progress(
    *,
    target: str,
    restored_bytes: int | None,
    total_bytes: int | None,
) -> str:
    return (
        f"Restoring {truncate(target)}: {bytes_amount(restored_bytes)} "
        f"of {bytes_amount(total_bytes)}."
    )


def hot_recovery_retry_other_disc(*, target: str) -> str:
    return (
        f"Riverhog could not restore {truncate(target)} from that disc. "
        "Try another registered disc or recovered media."
    )


def hot_recovery_done(*, target: str) -> str:
    return f"{truncate(target)} is back in hot storage."


# setup / doctor / billing / notification health


def doctor_ok() -> str:
    return "Riverhog setup looks healthy. No setup action is needed now."


def doctor_needs_attention(items: Sequence[str]) -> str:
    rendered = "\n".join(f"- {item}" for item in items)
    return f"Riverhog setup needs attention:\n{rendered}"


def billing_unavailable(*, reason: str | None = None) -> str:
    detail = f" Reason: {truncate(reason, max_chars=120)}." if reason else ""
    return (
        f"Recovery cost estimates are not available right now.{detail}\n"
        "Fix this before approving recovery work."
    )


def notification_health_failed(*, channel: str, latest_error: str | None = None) -> str:
    return (
        f"{channel} notifications may not be working."
        f"{_error_detail(latest_error)}\n"
        "Fix this so Riverhog can tell you when recovery or disc work needs attention."
    )


# action-needed notifications


def push_burn_work_ready(
    *,
    disc_count: int,
    oldest_ready_at: datetime | str | None = None,
) -> ActionNeededNotification:
    body = (
        f"Riverhog has {count_noun(disc_count, 'disc')} ready to write. "
        f"Run {command(ARC_DISC)} when you have blank discs."
    )
    if oldest_ready_at is not None:
        body += f" Oldest waiting since {when(oldest_ready_at)}."
    return ActionNeededNotification(
        event="images.ready",
        reminder_event="images.ready.reminder",
        title="Blank discs are needed",
        body=body,
        action=_run_arc_disc(),
        reminder_title="Disc work is still waiting",
        reminder_body=(
            f"Riverhog still has {count_noun(disc_count, 'disc')} waiting. "
            f"Run {command(ARC_DISC)} when you can."
        ),
        reminder_after=timedelta(hours=24),
    )


def push_disc_work_waiting_too_long(
    *,
    disc_count: int,
    oldest_ready_at: datetime | str | None,
) -> ActionNeededNotification:
    return ActionNeededNotification(
        event="operator.disc_work_waiting_too_long",
        title="Disc work is waiting",
        body=(
            f"{count_noun(disc_count, 'disc')} have been ready since "
            f"{when(oldest_ready_at)}. Run {command(ARC_DISC)} to write them."
        ),
        action=_run_arc_disc(),
        urgency="important",
        reminder_title="Disc work is still waiting",
        reminder_body=(
            f"Riverhog still has {count_noun(disc_count, 'disc')} waiting. "
            f"Run {command(ARC_DISC)}."
        ),
        reminder_after=timedelta(hours=24),
    )


def push_replacement_disc_needed(*, label_text: str | None = None) -> ActionNeededNotification:
    subject = f"label {label_text}" if label_text else "a protected copy"
    return ActionNeededNotification(
        event="operator.replacement_disc_needed",
        title="Replacement disc needed",
        body=(
            f"Riverhog needs a replacement disc for {subject}. "
            f"Run {command(ARC_DISC)} with a blank disc available."
        ),
        action=_run_arc_disc(),
        urgency="important",
        reminder_title="Replacement disc still needed",
        reminder_body=(
            f"Riverhog still needs a replacement disc for {subject}. "
            f"Run {command(ARC_DISC)}."
        ),
        reminder_after=timedelta(hours=24),
    )


def push_recovery_approval_required(
    *,
    affected: Iterable[str],
    estimated_cost: object | None,
) -> ActionNeededNotification:
    return ActionNeededNotification(
        event="operator.recovery_approval_required",
        title="Recovery needs approval",
        body=(
            f"Riverhog can recover {list_sentence(affected)}. "
            f"Estimated cost: {money_usd(estimated_cost)}. "
            f"Run {command(ARC_DISC)} to review."
        ),
        action=_run_arc_disc(),
        urgency="approval",
        reminder_title="Recovery is still waiting for approval",
        reminder_body=(
            f"Recovery for {list_sentence(affected)} still needs approval. "
            f"Run {command(ARC_DISC)}."
        ),
        reminder_after=timedelta(hours=6),
    )


def push_recovery_ready(
    *,
    affected: Iterable[str],
    expires_at: datetime | str | None,
) -> ActionNeededNotification:
    return ActionNeededNotification(
        event="images.rebuild_ready",
        reminder_event="images.rebuild_ready.reminder",
        title="Recovery is ready",
        body=(
            f"Recovered data for {list_sentence(affected)} is ready "
            f"{deadline(expires_at)}. Run {command(ARC_DISC)} before the window closes."
        ),
        action=_run_arc_disc(),
        urgency="time-sensitive",
        reminder_title="Recovery window is still open",
        reminder_body=(
            f"Recovered data for {list_sentence(affected)} is still ready "
            f"{deadline(expires_at)}. Run {command(ARC_DISC)}."
        ),
        reminder_after=timedelta(hours=1),
    )


def push_hot_recovery_needs_media(*, target: str) -> ActionNeededNotification:
    return ActionNeededNotification(
        event="operator.hot_recovery_needs_media",
        title="A disc is needed",
        body=(
            f"{truncate(target, max_chars=80)} is pinned for hot storage, but Riverhog "
            f"needs a disc to restore missing files. Run {command(ARC_DISC)}."
        ),
        action=_run_arc_disc(),
        reminder_title="Disc still needed",
        reminder_body=(
            "Riverhog still needs a disc to restore "
            f"{truncate(target, max_chars=80)} to hot storage. Run {command(ARC_DISC)}."
        ),
        reminder_after=timedelta(hours=24),
    )


def push_cloud_backup_failed(
    *,
    collection_id: str,
    attempts: int,
) -> ActionNeededNotification:
    return ActionNeededNotification(
        event="collections.glacier_upload.failed",
        title="Cloud backup needs attention",
        body=(
            f"Collection {collection_id} could not finish cloud backup after "
            f"{count_noun(attempts, 'try', 'tries')}. "
            f"Run {command(ARC)} to review the next safe step."
        ),
        action=_run_arc(),
        urgency="important",
        reminder_title="Cloud backup still needs attention",
        reminder_body=(
            f"Collection {collection_id} is still not fully protected. "
            f"Run {command(ARC)}."
        ),
        reminder_after=timedelta(hours=6),
    )


def push_notification_health_failed(*, channel: str) -> ActionNeededNotification:
    return ActionNeededNotification(
        event="operator.notification_health_failed",
        title="Notifications need attention",
        body=f"{channel} notifications may not be working. Run {command(ARC)}.",
        action=_run_arc(),
        urgency="important",
        reminder_title="Notifications still need attention",
        reminder_body=f"{channel} notifications still need attention. Run {command(ARC)}.",
        reminder_after=timedelta(hours=1),
    )


def push_billing_needs_attention(*, reason: str) -> ActionNeededNotification:
    return ActionNeededNotification(
        event="operator.billing_needs_attention",
        title="Recovery cost check needs attention",
        body=(
            "Riverhog could not confirm recovery cost information: "
            f"{truncate(reason, max_chars=100)}. Run {command(ARC)}."
        ),
        action=_run_arc(),
        urgency="important",
        reminder_title="Recovery cost check still needs attention",
        reminder_body=(
            "Riverhog still cannot confirm recovery cost information. "
            f"Run {command(ARC)} before approving recovery work."
        ),
        reminder_after=timedelta(hours=24),
    )


def push_setup_needs_attention(*, area: str, summary: str) -> ActionNeededNotification:
    return ActionNeededNotification(
        event="operator.setup_needs_attention",
        title="Setup needs attention",
        body=f"{area}: {truncate(summary, max_chars=100)}. Run {command(ARC)}.",
        action=_run_arc(),
        urgency="important",
        reminder_title="Setup still needs attention",
        reminder_body=f"{area} still needs attention. Run {command(ARC)}.",
        reminder_after=timedelta(hours=24),
    )
