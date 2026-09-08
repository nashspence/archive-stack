from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from http_api_contracts import exact_authority_page_operation, mutable_browse_operation
from riverhog_protocol import CollectionIdParameter, CollectionTag

from riverhog_api.auth import CatalogReader, CollectionTagManager
from riverhog_api.browse import (
    BrowsePageTokenQuery,
    BrowseQueryParameter,
    canonical_selectors,
    page_payload,
    page_position,
)
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.collections import (
    CollectionTagListOut,
    CollectionTagMembershipOut,
    CollectionTagMutationOut,
    CollectionTagMutationRequest,
    TagListOut,
)

router = APIRouter(tags=["collection-tags"])


@router.get(
    "/tags",
    response_model=TagListOut,
    openapi_extra=mutable_browse_operation(),
)
def list_tags(
    container: ContainerDep,
    principal: CatalogReader,
    page_size: int = Query(25, ge=1, le=100),
    page_token: BrowsePageTokenQuery = None,
    q: BrowseQueryParameter = None,
) -> TagListOut:
    selectors = canonical_selectors(q=q)
    position = page_position(
        container,
        principal=principal,
        operation="list_tags",
        page_token=page_token,
        selectors=selectors,
    )
    return TagListOut.model_validate(
        page_payload(
            container.collection_tags.list_tags(
                page_size=page_size,
                position=position,
                q=q,
                principal=principal,
            ),
            container=container,
            principal=principal,
            operation="list_tags",
            selectors=selectors,
        )
        | {"page_size": page_size}
    )


@router.get(
    "/collections/{collection_id}/tags",
    response_model=CollectionTagListOut,
    openapi_extra=exact_authority_page_operation(
        authority="collection-tag-set",
        authority_parameter="tag_set_identity",
        cursor_parameter="page_token",
        limit_parameter="page_size",
    ),
)
def list_collection_tags(
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CatalogReader,
    revision: Annotated[int, Query(ge=1)],
    tag_set_identity: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    page_size: int = Query(25, ge=1, le=100),
    page_token: BrowsePageTokenQuery = None,
) -> CollectionTagListOut:
    selectors = canonical_selectors(
        collection_id=collection_id,
        revision=revision,
        tag_set_identity=tag_set_identity,
    )
    position = page_position(
        container,
        principal=principal,
        operation="list_collection_tags",
        page_token=page_token,
        selectors=selectors,
    )
    return CollectionTagListOut.model_validate(
        page_payload(
            container.collection_tags.list_collection(
                collection_id,
                page_size=page_size,
                position=position,
                expected_revision=revision,
                expected_tag_set_identity=tag_set_identity,
                principal=principal,
            ),
            container=container,
            principal=principal,
            operation="list_collection_tags",
            selectors=selectors,
        )
        | {"page_size": page_size}
    )


@router.get(
    "/collections/{collection_id}/tags:contains",
    response_model=CollectionTagMembershipOut,
)
def collection_contains_tag(
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CatalogReader,
    tag: Annotated[CollectionTag, Query()],
    revision: Annotated[int, Query(ge=1)],
    tag_set_identity: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
) -> CollectionTagMembershipOut:
    return CollectionTagMembershipOut.model_validate(
        container.collection_tags.contains(
            collection_id,
            tag=tag,
            revision=revision,
            tag_set_identity=tag_set_identity,
            principal=principal,
        )
    )


@router.post(
    "/collections/{collection_id}/tags:add",
    response_model=CollectionTagMutationOut,
)
def add_collection_tag(
    collection_id: CollectionIdParameter,
    request: CollectionTagMutationRequest,
    container: ContainerDep,
    principal: CollectionTagManager,
) -> CollectionTagMutationOut:
    return CollectionTagMutationOut.model_validate(
        container.collection_tags.add(
            collection_id,
            tag=request.tag,
            operation_id=request.operation_id,
            expected_revision=request.expected_revision,
            expected_tag_set_identity=request.expected_tag_set_identity,
            principal=principal,
        )
    )


@router.post(
    "/collections/{collection_id}/tags:remove",
    response_model=CollectionTagMutationOut,
)
def remove_collection_tag(
    collection_id: CollectionIdParameter,
    request: CollectionTagMutationRequest,
    container: ContainerDep,
    principal: CollectionTagManager,
) -> CollectionTagMutationOut:
    return CollectionTagMutationOut.model_validate(
        container.collection_tags.remove(
            collection_id,
            tag=request.tag,
            operation_id=request.operation_id,
            expected_revision=request.expected_revision,
            expected_tag_set_identity=request.expected_tag_set_identity,
            principal=principal,
        )
    )


__all__ = ["router"]
