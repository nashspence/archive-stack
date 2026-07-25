from __future__ import annotations

from fastapi import APIRouter, Query

from riverhog_api.auth import CatalogReader, SlugCreator
from riverhog_api.deps import ContainerDep
from riverhog_api.schemas.slugs import CreateSlugRequest, SlugListOut, SlugOut

router = APIRouter(tags=["slugs"])


@router.post("/slugs", response_model=SlugOut)
def create_slug(
    request: CreateSlugRequest,
    container: ContainerDep,
    principal: SlugCreator,
) -> SlugOut:
    return SlugOut.model_validate(container.slugs.create(request.id, creator=principal))


@router.get("/slugs", response_model=SlugListOut)
def list_slugs(
    container: ContainerDep,
    principal: CatalogReader,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    sort: str = Query("id"),
    order: str = Query("asc"),
    q: str | None = Query(None),
    all_items: bool = Query(False, alias="all"),
) -> SlugListOut:
    return SlugListOut.model_validate(
        container.slugs.list(
            page=page,
            per_page=per_page,
            q=q,
            sort=sort,
            order=order,
            all_items=all_items,
            principal=principal,
        )
    )


@router.get("/slugs/{slug}", response_model=SlugOut)
def get_slug(
    slug: str,
    container: ContainerDep,
    principal: CatalogReader,
) -> SlugOut:
    return SlugOut.model_validate(container.slugs.get(slug, principal=principal))
