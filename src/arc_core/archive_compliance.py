from __future__ import annotations

from collections.abc import Iterable

from arc_core.domain.enums import CopyState, GlacierState, ProtectionState, VerificationState

DEFAULT_REQUIRED_PHYSICAL_COPIES = 2


def normalize_required_copy_count(required_copy_count: int | None) -> int:
    if isinstance(required_copy_count, int) and required_copy_count > 0:
        return required_copy_count
    return DEFAULT_REQUIRED_PHYSICAL_COPIES


def normalize_glacier_state(state: str | None) -> GlacierState:
    if state is None:
        return GlacierState.PENDING
    try:
        return GlacierState(state)
    except ValueError:
        return GlacierState.PENDING


def normalize_copy_state(state: str | None) -> CopyState:
    if state is None:
        return CopyState.REGISTERED
    try:
        return CopyState(state)
    except ValueError:
        return CopyState.REGISTERED


def normalize_verification_state(state: str | None) -> VerificationState:
    if state is None:
        return VerificationState.PENDING
    try:
        return VerificationState(state)
    except ValueError:
        return VerificationState.PENDING


def copy_counts_toward_protection(state: str | None) -> bool:
    normalized = normalize_copy_state(state)
    return normalized in {CopyState.VERIFIED, CopyState.REGISTERED}


def copy_counts_as_verified(*, state: str | None, verification_state: str | None) -> bool:
    return copy_counts_toward_protection(state) and (
        normalize_verification_state(verification_state) == VerificationState.VERIFIED
    )


def registered_copy_shortfall(*, required_copy_count: int, registered_copy_count: int) -> int:
    return max(required_copy_count - registered_copy_count, 0)


def image_protection_state(
    *,
    required_copy_count: int,
    registered_copy_count: int,
    glacier_state: GlacierState,
) -> ProtectionState:
    if registered_copy_count >= required_copy_count and glacier_state == GlacierState.UPLOADED:
        return ProtectionState.PROTECTED
    if registered_copy_count > 0 or glacier_state != GlacierState.PENDING:
        return ProtectionState.PARTIALLY_PROTECTED
    return ProtectionState.UNPROTECTED


def collection_protection_state(
    *,
    bytes_total: int,
    protected_bytes: int,
    archived_bytes: int,
    image_states: Iterable[ProtectionState],
) -> ProtectionState:
    states = tuple(image_states)
    if bytes_total > 0 and protected_bytes >= bytes_total:
        return ProtectionState.PROTECTED
    if (
        protected_bytes > 0
        or archived_bytes > 0
        or any(state != ProtectionState.UNPROTECTED for state in states)
    ):
        return ProtectionState.PARTIALLY_PROTECTED
    return ProtectionState.UNPROTECTED
