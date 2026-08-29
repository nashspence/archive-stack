from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response
from riverhog_protocol import CollectionIdParameter, SortOrder, TagSort
from riverhog_protocol.paths import CanonicalTag

from riverhog_api.auth import CatalogReader, CollectionTagManager, TagCreator, TagDeleter
from riverhog_api.complete_enumeration import (
    CompleteEnumerationResponse,
    bounded_list_operation,
    complete_enumeration_operation,
    complete_enumeration_response,
)
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.tags import (
    CollectionTagMembershipOut,
    CollectionTagSetOut,
    CollectionTagsOut,
    CreateTagRequest,
    DeleteTagRequest,
    MutateCollectionTagRequest,
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
    openapi_extra=bounded_list_operation(paired_operation_id="stream_tags"),
)
def list_tags(
    container: ContainerDep,
    principal: CatalogReader,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    q: str | None = Query(None),
    sort: Annotated[TagSort, Query()] = "id",
    order: Annotated[SortOrder, Query()] = "asc",
) -> TagListOut:
    return TagListOut.model_validate(
        container.tags.list(
            page=page,
            per_page=per_page,
            q=q,
            sort=sort,
            order=order,
            principal=principal,
        )
    )


@router.get(
    "/tags/stream",
    response_class=CompleteEnumerationResponse,
    openapi_extra=complete_enumeration_operation(
        paired_operation_id="list_tags",
        item_type=TagOut,
        schema_id="riverhog.tag/v1",
    ),
)
def stream_tags(
    container: ContainerDep,
    principal: CatalogReader,
    q: str | None = Query(None),
    sort: Annotated[TagSort, Query()] = "id",
    order: Annotated[SortOrder, Query()] = "asc",
) -> Response:
    query = {"q": q, "sort": sort, "order": order}
    return complete_enumeration_response(
        container.tags.iter_tags(q=q, sort=sort, order=order, principal=principal),
        query=query,
        item_type=TagOut,
        schema_id="riverhog.tag/v1",
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
    openapi_extra=bounded_list_operation(paired_operation_id="stream_collection_tags"),
)
def get_collection_tags(
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CatalogReader,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
) -> CollectionTagsOut:
    return CollectionTagsOut.model_validate(
        container.tags.list_collection_tags(
            collection_id,
            page=page,
            per_page=per_page,
            principal=principal,
        )
    )


@router.get(
    "/collections/{collection_id}/tags/stream",
    response_class=CompleteEnumerationResponse,
    openapi_extra=complete_enumeration_operation(
        paired_operation_id="get_collection_tags",
        item_type=CollectionTagMembershipOut,
        schema_id="riverhog.collection-tag-membership/v1",
    ),
)
def stream_collection_tags(
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CatalogReader,
) -> Response:
    return complete_enumeration_response(
        container.tags.iter_collection_tags(collection_id, principal=principal),
        query={"collection_id": collection_id},
        item_type=CollectionTagMembershipOut,
        schema_id="riverhog.collection-tag-membership/v1",
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
