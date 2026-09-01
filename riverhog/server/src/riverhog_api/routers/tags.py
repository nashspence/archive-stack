from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from http_api_contracts import mutable_browse_operation
from riverhog_protocol import CollectionIdParameter, SortOrder, TagSort
from riverhog_protocol.paths import CanonicalTag

from riverhog_api.auth import CatalogReader, CollectionTagManager, TagCreator, TagDeleter
from riverhog_api.browse import (
    BrowsePageTokenQuery,
    BrowseQueryParameter,
    canonical_selectors,
    page_payload,
    page_position,
)
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.tags import (
    CollectionTagSetOut,
    CollectionTagsOut,
    CreateTagRequest,
    DeleteTagRequest,
    MutateCollectionTagRequest,
    ReplaceCollectionTagsRequest,
    TagDeletionPlanOut,
    TagDeletionResultOut,
    TagListOut,
    TagOut,
)

router = APIRouter(tags=["tags"])


@router.post("/tags", response_model=TagOut)
def create_tag(
    request: CreateTagRequest,
    container: ContainerDep,
    principal: TagCreator,
) -> TagOut:
    return TagOut.model_validate(container.tags.create(request.id, creator=principal))


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
    sort: Annotated[TagSort, Query()] = "id",
    order: Annotated[SortOrder, Query()] = "asc",
) -> TagListOut:
    selectors = canonical_selectors(q=q, sort=sort, order=order)
    position = page_position(
        container,
        principal=principal,
        operation="list_tags",
        page_token=page_token,
        selectors=selectors,
    )
    return TagListOut.model_validate(
        page_payload(
            container.tags.list(
                page_size=page_size,
                position=position,
                q=q,
                sort=sort,
                order=order,
                principal=principal,
            ),
            container=container,
            principal=principal,
            operation="list_tags",
            selectors=selectors,
        )
    )


@router.get("/tags/{tag}", response_model=TagOut)
def get_tag(
    tag: CanonicalTag,
    container: ContainerDep,
    principal: CatalogReader,
) -> TagOut:
    return TagOut.model_validate(container.tags.get(tag, principal=principal))


@router.post("/tags/{tag}/deletion-plan", response_model=TagDeletionPlanOut)
def plan_tag_deletion(
    tag: CanonicalTag,
    container: ContainerDep,
    _principal: TagDeleter,
) -> TagDeletionPlanOut:
    return TagDeletionPlanOut.model_validate(container.tags.plan_deletion(tag))


@router.post("/tags/{tag}/delete", response_model=TagDeletionResultOut)
def delete_tag(
    tag: CanonicalTag,
    request: DeleteTagRequest,
    container: ContainerDep,
    _principal: TagDeleter,
) -> TagDeletionResultOut:
    return TagDeletionResultOut.model_validate(
        container.tags.delete(tag, challenge=request.challenge)
    )


@router.get(
    "/collections/{collection_id}/tags",
    response_model=CollectionTagsOut,
    openapi_extra=mutable_browse_operation(),
)
def get_collection_tags(
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CatalogReader,
    page_size: int = Query(25, ge=1, le=100),
    page_token: BrowsePageTokenQuery = None,
) -> CollectionTagsOut:
    selectors = canonical_selectors(collection_id=collection_id)
    position = page_position(
        container,
        principal=principal,
        operation="get_collection_tags",
        page_token=page_token,
        selectors=selectors,
    )
    return CollectionTagsOut.model_validate(
        page_payload(
            container.tags.list_collection_tags(
                collection_id,
                page_size=page_size,
                position=position,
                principal=principal,
            ),
            container=container,
            principal=principal,
            operation="get_collection_tags",
            selectors=selectors,
        )
    )


@router.post("/collections/{collection_id}/tags/{tag}", response_model=CollectionTagSetOut)
def add_collection_tag(
    collection_id: CollectionIdParameter,
    tag: CanonicalTag,
    request: MutateCollectionTagRequest,
    container: ContainerDep,
    principal: CollectionTagManager,
) -> CollectionTagSetOut:
    return CollectionTagSetOut.model_validate(
        container.tags.add_collection_tag(
            collection_id,
            tag,
            principal=principal,
            event_context=request.event_context,
        )
    )


@router.put("/collections/{collection_id}/tags", response_model=CollectionTagSetOut)
def replace_collection_tags(
    collection_id: CollectionIdParameter,
    request: ReplaceCollectionTagsRequest,
    container: ContainerDep,
    principal: CollectionTagManager,
) -> CollectionTagSetOut:
    return CollectionTagSetOut.model_validate(
        container.tags.replace_collection_tags(
            collection_id,
            request.tags,
            principal=principal,
            event_context=request.event_context,
        )
    )


@router.delete("/collections/{collection_id}/tags/{tag}", response_model=CollectionTagSetOut)
def remove_collection_tag(
    collection_id: CollectionIdParameter,
    tag: CanonicalTag,
    request: MutateCollectionTagRequest,
    container: ContainerDep,
    principal: CollectionTagManager,
) -> CollectionTagSetOut:
    return CollectionTagSetOut.model_validate(
        container.tags.remove_collection_tag(
            collection_id,
            tag,
            principal=principal,
            event_context=request.event_context,
        )
    )
