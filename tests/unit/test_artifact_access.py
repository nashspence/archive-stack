from __future__ import annotations

from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_core.artifact_access import artifact_scope_filter
from riverhog_core.catalog_models import CollectionFileRecord
from sqlalchemy import select


def test_persisted_artifact_scope_compiles_to_one_correlated_authority_lookup() -> None:
    principal = ApplicationPrincipal(
        app="claim:fixture",
        key_id="fixture-key",
        access=frozenset(),
        artifact_scope_capability_id="a" * 32,
    )
    statement = select(CollectionFileRecord.path).where(
        artifact_scope_filter(
            CollectionFileRecord.collection_id,
            CollectionFileRecord.path,
            principal,
        )
    )
    compiled = statement.compile()

    assert set(compiled.params.values()) == {"a" * 32}
    assert "EXISTS" in str(compiled)
    assert "collection_transform_capability_artifacts" in str(compiled)


def test_unscoped_principal_does_not_add_an_artifact_predicate() -> None:
    predicate = artifact_scope_filter(
        CollectionFileRecord.collection_id,
        CollectionFileRecord.path,
        ApplicationPrincipal(app="reader", key_id="reader", access=frozenset()),
    )

    assert str(predicate.compile()).lower() == "true"
