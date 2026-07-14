from __future__ import annotations

from riverhog_core.domain.enums import ArchiveState, CoverageState, DiscState, VerificationState

DEFAULT_REQUIRED_DISCS = 2


def normalize_required_disc_count(required_disc_count: int | None) -> int:
    if isinstance(required_disc_count, int) and required_disc_count > 0:
        return required_disc_count
    return DEFAULT_REQUIRED_DISCS


def normalize_archive_state(state: str | None) -> ArchiveState:
    if state is None:
        return ArchiveState.PENDING
    try:
        return ArchiveState(state)
    except ValueError:
        return ArchiveState.PENDING


def normalize_disc_state(state: str | None) -> DiscState:
    if state is None:
        return DiscState.REGISTERED
    try:
        return DiscState(state)
    except ValueError:
        return DiscState.REGISTERED


def normalize_verification_state(state: str | None) -> VerificationState:
    if state is None:
        return VerificationState.PENDING
    try:
        return VerificationState(state)
    except ValueError:
        return VerificationState.PENDING


def disc_counts_toward_redundancy(state: str | None) -> bool:
    normalized = normalize_disc_state(state)
    return normalized in {DiscState.VERIFIED, DiscState.REGISTERED}


def disc_counts_as_verified(*, state: str | None, verification_state: str | None) -> bool:
    return disc_counts_toward_redundancy(state) and (
        normalize_verification_state(verification_state) == VerificationState.VERIFIED
    )


def registered_disc_shortfall(*, required_disc_count: int, registered_disc_count: int) -> int:
    return max(required_disc_count - registered_disc_count, 0)


def disc_redundancy_state(
    *,
    required_disc_count: int,
    registered_disc_count: int,
) -> CoverageState:
    if registered_disc_count >= required_disc_count:
        return CoverageState.FULL
    if registered_disc_count > 0:
        return CoverageState.PARTIAL
    return CoverageState.NONE


def coverage_state(*, total_bytes: int, covered_bytes: int) -> CoverageState:
    if total_bytes > 0 and covered_bytes >= total_bytes:
        return CoverageState.FULL
    if covered_bytes > 0:
        return CoverageState.PARTIAL
    return CoverageState.NONE
