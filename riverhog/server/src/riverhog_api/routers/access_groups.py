from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from http_api_contracts import mutable_browse_operation
from riverhog_protocol import (
    CollectionAccessGroupSort,
    CollectionAccessGroupStatus,
    CollectionIdParameter,
    SortOrder,
)

from riverhog_api.auth import CollectionAccessGroupManager
from riverhog_api.browse import (
    BrowsePageTokenQuery,
    BrowseQueryParameter,
    canonical_selectors,
    page_payload,
    page_position,
)
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.access_groups import (
    CollectionAccessGroupCreateIn,
    CollectionAccessGroupListOut,
    CollectionAccessGroupMembershipOut,
    CollectionAccessGroupMembersOut,
    CollectionAccessGroupOut,
    CollectionAccessGroupsForCollectionOut,
    CollectionAccessGroupUpdateIn,
)

router = APIRouter(tags=["collection-access-groups"])


@router.post("/collection-access-groups", response_model=CollectionAccessGroupOut)
def create_collection_access_group(
    request: CollectionAccessGroupCreateIn,
    container: ContainerDep,
    principal: CollectionAccessGroupManager,
) -> CollectionAccessGroupOut:
    return CollectionAccessGroupOut.model_validate(
        container.access_groups.create(
            idempotency_key=request.idempotency_key,
            display_label=request.display_label,
            creator=principal,
        )
    )


@router.get(
    "/collection-access-groups",
    response_model=CollectionAccessGroupListOut,
    openapi_extra=mutable_browse_operation(),
)
def list_collection_access_groups(
    container: ContainerDep,
    principal: CollectionAccessGroupManager,
    page_size: int = Query(25, ge=1, le=100),
    page_token: BrowsePageTokenQuery = None,
    q: BrowseQueryParameter = None,
    status: Annotated[CollectionAccessGroupStatus | None, Query()] = None,
    sort: Annotated[CollectionAccessGroupSort, Query()] = "id",
    order: Annotated[SortOrder, Query()] = "asc",
) -> CollectionAccessGroupListOut:
    selectors = canonical_selectors(q=q, status=status, sort=sort, order=order)
    position = page_position(
        container,
        principal=principal,
        operation="list_collection_access_groups",
        page_token=page_token,
        selectors=selectors,
    )
    return CollectionAccessGroupListOut.model_validate(
        page_payload(
            container.access_groups.list(
                page_size=page_size,
                position=position,
                q=q,
                status=status,
                sort=sort,
                order=order,
            ),
            container=container,
            principal=principal,
            operation="list_collection_access_groups",
            selectors=selectors,
        )
    )


@router.get("/collection-access-groups/{group_id}", response_model=CollectionAccessGroupOut)
def get_collection_access_group(
    group_id: str,
    container: ContainerDep,
    _principal: CollectionAccessGroupManager,
) -> CollectionAccessGroupOut:
    return CollectionAccessGroupOut.model_validate(container.access_groups.get(group_id))


@router.put("/collection-access-groups/{group_id}", response_model=CollectionAccessGroupOut)
def update_collection_access_group(
    group_id: str,
    request: CollectionAccessGroupUpdateIn,
    container: ContainerDep,
    _principal: CollectionAccessGroupManager,
) -> CollectionAccessGroupOut:
    return CollectionAccessGroupOut.model_validate(
        container.access_groups.update(
            group_id,
            display_label=request.display_label,
            status=request.status,
        )
    )


@router.get(
    "/collection-access-groups/{group_id}/collections",
    response_model=CollectionAccessGroupMembersOut,
    openapi_extra=mutable_browse_operation(),
)
def list_collection_access_group_members(
    group_id: str,
    container: ContainerDep,
    principal: CollectionAccessGroupManager,
    page_size: int = Query(25, ge=1, le=100),
    page_token: BrowsePageTokenQuery = None,
) -> CollectionAccessGroupMembersOut:
    selectors = canonical_selectors(group_id=group_id)
    position = page_position(
        container,
        principal=principal,
        operation="list_collection_access_group_members",
        page_token=page_token,
        selectors=selectors,
    )
    return CollectionAccessGroupMembersOut.model_validate(
        page_payload(
            container.access_groups.list_members(
                group_id,
                page_size=page_size,
                position=position,
            ),
            container=container,
            principal=principal,
            operation="list_collection_access_group_members",
            selectors=selectors,
        )
    )


@router.get(
    "/collections/{collection_id}/access-groups",
    response_model=CollectionAccessGroupsForCollectionOut,
    openapi_extra=mutable_browse_operation(),
)
def list_collection_access_groups_for_collection(
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CollectionAccessGroupManager,
    page_size: int = Query(25, ge=1, le=100),
    page_token: BrowsePageTokenQuery = None,
) -> CollectionAccessGroupsForCollectionOut:
    selectors = canonical_selectors(collection_id=collection_id)
    position = page_position(
        container,
        principal=principal,
        operation="list_collection_access_groups_for_collection",
        page_token=page_token,
        selectors=selectors,
    )
    return CollectionAccessGroupsForCollectionOut.model_validate(
        page_payload(
            container.access_groups.list_collection_groups(
                collection_id,
                page_size=page_size,
                position=position,
            ),
            container=container,
            principal=principal,
            operation="list_collection_access_groups_for_collection",
            selectors=selectors,
        )
    )


@router.put(
    "/collection-access-groups/{group_id}/collections/{collection_id}",
    response_model=CollectionAccessGroupMembershipOut,
)
def add_collection_access_group_member(
    group_id: str,
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CollectionAccessGroupManager,
) -> CollectionAccessGroupMembershipOut:
    return CollectionAccessGroupMembershipOut.model_validate(
        container.access_groups.add_member(group_id, collection_id, principal=principal)
    )


@router.delete(
    "/collection-access-groups/{group_id}/collections/{collection_id}",
    response_model=CollectionAccessGroupMembershipOut,
)
def remove_collection_access_group_member(
    group_id: str,
    collection_id: CollectionIdParameter,
    container: ContainerDep,
    principal: CollectionAccessGroupManager,
) -> CollectionAccessGroupMembershipOut:
    return CollectionAccessGroupMembershipOut.model_validate(
        container.access_groups.remove_member(group_id, collection_id, principal=principal)
    )


__all__ = ["router"]
