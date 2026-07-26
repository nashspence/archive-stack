from __future__ import annotations

from fastapi import APIRouter, Query

from riverhog_api.auth import CatalogReader, CollectionTagManager, TagCreator
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.tags import (
    CollectionTagsOut,
    CreateTagRequest,
    ReplaceCollectionTagsRequest,
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
    sort: str = Query("id"),
    order: str = Query("asc"),
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
    tag: str,
    container: ContainerDep,
    principal: CatalogReader,
) -> TagOut:
    return TagOut.model_validate(container.tags.get(tag, principal=principal))


@router.get("/collections/{collection_id}/tags", response_model=CollectionTagsOut)
def get_collection_tags(
    collection_id: int,
    container: ContainerDep,
    principal: CatalogReader,
) -> CollectionTagsOut:
    return CollectionTagsOut.model_validate(
        container.tags.get_collection(collection_id, principal=principal)
    )


@router.put("/collections/{collection_id}/tags", response_model=CollectionTagsOut)
def replace_collection_tags(
    collection_id: int,
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
