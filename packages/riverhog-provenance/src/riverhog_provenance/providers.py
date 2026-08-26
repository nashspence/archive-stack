"""Explicit composition of installed provenance observer and contract providers."""

from __future__ import annotations

import importlib.metadata
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from riverhog_provenance_contracts import (
    PROVENANCE_CONTRACT_ENTRY_POINT_GROUP,
    SHA256_PATTERN,
    ProvenanceContractBinding,
)

from .interface import FileStateObserver
from .model import ObservationRequest, ObservationResult
from .schema import compile_observer_contract_validator

PROVENANCE_OBSERVER_ENTRY_POINT_GROUP = "riverhog.provenance-observers"
PROVENANCE_OBSERVER_BINDING_FORMAT = "riverhog-provenance-observer-binding/v1"
PROVENANCE_OBSERVER_REFERENCE_FORMAT = "riverhog-provenance-observer-reference/v1"
type FileStateObserverFactory = Callable[[], FileStateObserver]


@dataclass(frozen=True, slots=True)
class ProvenanceObserverBinding:
    """One observer implementation bound to one exact external contract pack."""

    observer_id: str
    contract_provider: str
    contract_id: str
    contract_sha256: str
    factory: FileStateObserverFactory
    format: str = PROVENANCE_OBSERVER_BINDING_FORMAT

    def __post_init__(self) -> None:
        canonical = {
            "observer_id": self.observer_id,
            "contract_provider": self.contract_provider,
            "contract_id": self.contract_id,
        }
        if any(not value or value != value.strip() for value in canonical.values()):
            raise ValueError("provenance observer binding identities must be canonical")
        if re.fullmatch(SHA256_PATTERN, self.contract_sha256) is None:
            raise ValueError("provenance observer contract digest must be SHA-256")
        if self.format != PROVENANCE_OBSERVER_BINDING_FORMAT:
            raise ValueError("unsupported provenance observer binding format")
        if not callable(self.factory):
            raise TypeError("provenance observer factory must be callable")


@dataclass(frozen=True, slots=True)
class ProvenanceProviderMetadata:
    """Safe installed-distribution metadata that does not execute provider code."""

    name: str
    value: str
    distribution: str | None
    version: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "entry_point": self.value,
            "distribution": self.distribution,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class ResolvedProvenanceObserver:
    """One explicitly selected observer plus its exact separately owned contract."""

    name: str
    metadata: ProvenanceProviderMetadata
    binding: ProvenanceObserverBinding
    contract: ProvenanceContractBinding
    _validator: Callable[[Mapping[str, Any]], None] = field(repr=False)

    def create(self) -> FileStateObserver:
        observer = self.binding.factory()
        if not isinstance(observer, FileStateObserver):
            raise TypeError(f"provenance observer factory returned an invalid object: {self.name}")
        return _ContractValidatedObserver(
            observer,
            observer_reference=self.observer_reference(),
            validator=self._validator,
        )

    def observer_reference(self) -> dict[str, object]:
        """Return the exact implementation and contract identity retained at capture."""

        reference: dict[str, object] = {
            "format": PROVENANCE_OBSERVER_REFERENCE_FORMAT,
            "provider": self.name,
            "observer_id": self.binding.observer_id,
            "contract": self.contract.reference(self.binding.contract_provider),
        }
        if self.metadata.distribution is not None and self.metadata.version is not None:
            reference["distribution"] = self.metadata.distribution
            reference["version"] = self.metadata.version
        return reference

    def as_dict(self) -> dict[str, object]:
        return {
            "format": PROVENANCE_OBSERVER_BINDING_FORMAT,
            **self.metadata.as_dict(),
            "observer_id": self.binding.observer_id,
            "contract_provider": self.binding.contract_provider,
            "contract_id": self.contract.contract_id,
            "contract_sha256": self.contract.contract_sha256,
            "schema_ids": sorted(self.contract.schemas),
        }


class _ContractValidatedObserver:
    def __init__(
        self,
        observer: FileStateObserver,
        *,
        observer_reference: Mapping[str, object],
        validator: Callable[[Mapping[str, Any]], None],
    ) -> None:
        self._observer = observer
        self._observer_reference = dict(observer_reference)
        self._validator = validator
        self.platform_family = observer.platform_family

    def observe(self, request: ObservationRequest) -> ObservationResult:
        result = self._observer.observe(request)
        if not isinstance(result, ObservationResult):
            raise TypeError("provenance observer returned an invalid observation result")
        capture = dict(result.capture)
        detail = dict(capture.get("detail", {}))
        detail["provenance_observer"] = dict(self._observer_reference)
        capture["detail"] = detail
        accepted = ObservationResult(
            state=result.state,
            capture=capture,
            environment=result.environment,
            agents=result.agents,
            payload_bindings=result.payload_bindings,
            extensions=result.extensions,
        )
        self._validator(accepted.graph_fragment())
        return accepted


def _entry_points(group: str) -> tuple[importlib.metadata.EntryPoint, ...]:
    return tuple(importlib.metadata.entry_points(group=group))


def _metadata(entry_point: importlib.metadata.EntryPoint) -> ProvenanceProviderMetadata:
    distribution = getattr(entry_point, "dist", None)
    return ProvenanceProviderMetadata(
        name=entry_point.name,
        value=entry_point.value,
        distribution=distribution.name if distribution is not None else None,
        version=distribution.version if distribution is not None else None,
    )


def list_provenance_observers() -> tuple[ProvenanceProviderMetadata, ...]:
    """List installed observer entry-point metadata without executing provider code."""

    return tuple(
        sorted(
            (
                _metadata(entry_point)
                for entry_point in _entry_points(PROVENANCE_OBSERVER_ENTRY_POINT_GROUP)
            ),
            key=lambda item: (item.name, item.distribution or "", item.value),
        )
    )


def _resolve_exact_entry_point(group: str, name: str) -> importlib.metadata.EntryPoint:
    canonical = name.strip()
    if not canonical or canonical != name:
        raise ValueError("provenance provider name must be nonempty and canonical")
    matches = [entry_point for entry_point in _entry_points(group) if entry_point.name == name]
    if len(matches) != 1:
        raise ValueError(f"provenance provider must resolve exactly once: {name}")
    return matches[0]


def resolve_provenance_observer(name: str) -> ResolvedProvenanceObserver:
    """Load only one explicitly selected observer and its exact contract provider."""

    observer_entry_point = _resolve_exact_entry_point(PROVENANCE_OBSERVER_ENTRY_POINT_GROUP, name)
    binding = observer_entry_point.load()
    if not isinstance(binding, ProvenanceObserverBinding):
        raise TypeError(f"provenance observer provider has an invalid binding: {name}")
    contract_entry_point = _resolve_exact_entry_point(
        PROVENANCE_CONTRACT_ENTRY_POINT_GROUP,
        binding.contract_provider,
    )
    contract = contract_entry_point.load()
    if not isinstance(contract, ProvenanceContractBinding):
        raise TypeError(
            f"provenance contract provider has an invalid binding: {binding.contract_provider}"
        )
    if (
        contract.contract_id != binding.contract_id
        or contract.contract_sha256 != binding.contract_sha256
    ):
        raise ValueError("provenance observer and contract provider identities disagree")
    for schema in contract.schemas.values():
        Draft202012Validator.check_schema(schema)
    return ResolvedProvenanceObserver(
        name=name,
        metadata=_metadata(observer_entry_point),
        binding=binding,
        contract=contract,
        _validator=compile_observer_contract_validator(contract.schemas),
    )


__all__ = [
    "FileStateObserverFactory",
    "PROVENANCE_OBSERVER_BINDING_FORMAT",
    "PROVENANCE_OBSERVER_ENTRY_POINT_GROUP",
    "PROVENANCE_OBSERVER_REFERENCE_FORMAT",
    "ProvenanceObserverBinding",
    "ProvenanceProviderMetadata",
    "ResolvedProvenanceObserver",
    "list_provenance_observers",
    "resolve_provenance_observer",
]
