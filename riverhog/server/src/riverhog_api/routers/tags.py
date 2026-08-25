from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from riverhog_protocol import CollectionIdParameter, SortOrder, TagSort
from riverhog_protocol.paths import CanonicalTag

from riverhog_api.auth import CatalogReader, CollectionTagManager, TagCreator, TagDeleter
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.tags import (
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


@router.get("/tags", response_model=TagListOut)
def list_tags(
    container: ContainerDep,
    principal: CatalogReader,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    q: str | None = Query(None),
    sort: Annotated[TagSort, Query()] = "id",
    order: Annotated[SortOrder, Query()] = "asc",
    all_items: bool = Query(False, alias="all"),
) -> TagListOut:
    return TagListOut.model_validate(
        container.tags.list(
            page=page,
            per_page=per_page,
            q=q,
            sort=sort,
            order=order,
            all_items=all_items,
            principal=principal,
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


@router.get("/collections/{collection_id}/tags", response_model=CollectionTagsOut)
def get_collection_tags(
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CatalogReader,
) -> CollectionTagsOut:
    return CollectionTagsOut.model_validate(
        container.tags.get_collection(collection_id, principal=principal)
    )


@router.put("/collections/{collection_id}/tags", response_model=CollectionTagsOut)
def replace_collection_tags(
    collection_id: CollectionIdParameter,
    request: ReplaceCollectionTagsRequest,
    container: ContainerDep,
    principal: CollectionTagManager,
) -> CollectionTagsOut:
    return CollectionTagsOut.model_validate(
        container.tags.replace_collection(
            collection_id,
            request.tags,
            principal=principal,
            event_context=request.event_context,
        )
    )


@router.post("/collections/{collection_id}/tags/{tag}", response_model=CollectionTagsOut)
def add_collection_tag(
    collection_id: CollectionIdParameter,
    tag: CanonicalTag,
    request: MutateCollectionTagRequest,
    container: ContainerDep,
    principal: CollectionTagManager,
) -> CollectionTagsOut:
    return CollectionTagsOut.model_validate(
        container.tags.add_collection_tag(
            collection_id,
            tag,
            principal=principal,
            event_context=request.event_context,
        )
    )


@router.delete("/collections/{collection_id}/tags/{tag}", response_model=CollectionTagsOut)
def remove_collection_tag(
    collection_id: CollectionIdParameter,
    tag: CanonicalTag,
    request: MutateCollectionTagRequest,
    container: ContainerDep,
    principal: CollectionTagManager,
) -> CollectionTagsOut:
    return CollectionTagsOut.model_validate(
        container.tags.remove_collection_tag(
            collection_id,
            tag,
            principal=principal,
            event_context=request.event_context,
        )
    )
