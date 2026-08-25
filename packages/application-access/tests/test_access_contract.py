from __future__ import annotations

import pytest
from application_access import (
    CATALOG_READ,
    EVENTS_READ,
    EVENTS_READ_ALL,
    PROVENANCE_EXPORT,
    PROVENANCE_READ,
    ApplicationAccess,
    ApplicationAccessError,
    ApplicationAccessGrant,
    ApplicationAccessGrantSet,
    ApplicationKeyId,
    ApplicationName,
    collection_resource,
    normalize_access,
    permission_resources,
    tag_resource,
)
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError


def test_public_access_contract_normalizes_and_covers_grants() -> None:
    access = normalize_access(
        (
            ApplicationAccess(EVENTS_READ_ALL),
            ApplicationAccess(PROVENANCE_EXPORT, "tag:docs"),
            ApplicationAccess(CATALOG_READ, "collection:42"),
        )
    )

    assert permission_resources(access, EVENTS_READ) == {"*"}
    assert permission_resources(access, PROVENANCE_READ) == {"tag:docs"}
    assert collection_resource(42) == "collection:42"
    assert tag_resource("docs") == "tag:docs"


def test_public_access_models_enforce_grant_and_set_relationships() -> None:
    assert ApplicationAccessGrant(
        permission="collections:create",
        resource="tag:incoming",
    ).as_access() == ApplicationAccess("collections:create", "tag:incoming")

    with pytest.raises(ApplicationAccessError, match="does not accept a scoped resource"):
        ApplicationAccess("keys:manage", "tag:incoming")
    with pytest.raises(ValueError, match="must target a tag"):
        ApplicationAccessGrant(
            permission="collections:create",
            resource="collection:1",
        )
    with pytest.raises(ValueError, match="used alone"):
        ApplicationAccessGrantSet.model_validate(
            [
                {"permission": "*", "resource": "*"},
                {"permission": "catalog:read", "resource": "*"},
            ]
        )
    with pytest.raises(ValueError, match="must not contain duplicates"):
        ApplicationAccessGrantSet.model_validate(
            [
                {"permission": "catalog:read", "resource": "*"},
                {"permission": "catalog:read", "resource": "*"},
            ]
        )


def test_access_grant_set_schema_projects_relational_rules() -> None:
    validator = Draft202012Validator(ApplicationAccessGrantSet.model_json_schema())

    assert not list(
        validator.iter_errors([{"permission": "catalog:read", "resource": "collection:1"}])
    )
    assert list(validator.iter_errors([{"permission": "keys:manage", "resource": "collection:1"}]))
    assert list(
        validator.iter_errors(
            [
                {"permission": "*", "resource": "*"},
                {"permission": "catalog:read", "resource": "*"},
            ]
        )
    )


@pytest.mark.parametrize("value", ("indexer", "media-indexer", "app42"))
def test_application_public_identities_accept_only_canonical_values(value: str) -> None:
    assert TypeAdapter(ApplicationName).validate_python(value, strict=True) == value
    assert TypeAdapter(ApplicationKeyId).validate_python("0123456789abcdef", strict=True) == (
        "0123456789abcdef"
    )


@pytest.mark.parametrize("value", ("Indexer", " indexer", "media_indexer", "media--indexer"))
def test_application_name_rejects_aliases(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ApplicationName).validate_python(value, strict=True)


@pytest.mark.parametrize("value", ("ABCDEF0123456789", "short", " 0123456789abcdef"))
def test_application_key_id_rejects_aliases(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ApplicationKeyId).validate_python(value, strict=True)
