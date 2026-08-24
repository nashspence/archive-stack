from __future__ import annotations

from typing import Annotated, Any, cast
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import APIRouter, Path, Query, Request, Response
from http_api_contracts import operation_interface
from riverhog_protocol import PortableCollectionRecord, portable_collection_json_schema

from riverhog_api.auth import CatalogReader
from riverhog_api.deps import ContainerDep

router = APIRouter(tags=["catalog"])
_RS = "http://www.openarchives.org/rs/terms/"
_SITEMAP = "http://www.sitemaps.org/schemas/sitemap/0.9"
_RESOURCE_LIST_PAGE_SIZE = 10_000


def _xml_responses() -> dict[int | str, dict[str, Any]]:
    return {
        200: {
            "description": "OK",
            "content": {"application/xml": {"schema": {"type": "string"}}},
        }
    }


def _url(request: Request, path: str) -> str:
    app = request.scope.get("app")
    state = getattr(app, "state", None)
    configured = getattr(state, "public_base_url", None)
    base_url = str(configured) if configured else str(request.base_url)
    return base_url.rstrip("/") + path


def _xml(root: Element) -> Response:
    return Response(
        content=tostring(root, encoding="utf-8", xml_declaration=True),
        media_type="application/xml",
    )


@router.get(
    "/.well-known/resourcesync",
    response_class=Response,
    responses=_xml_responses(),
    openapi_extra=operation_interface("standard-tool/protocol"),
)
def well_known_resourcesync(
    request: Request,
    principal: CatalogReader,
) -> Response:
    root = Element("urlset", {"xmlns": _SITEMAP, "xmlns:rs": _RS})
    url = SubElement(root, "url")
    SubElement(url, "loc").text = _url(request, "/resourcesync/capabilitylist.xml")
    metadata = SubElement(url, "rs:md")
    metadata.set("capability", "capabilitylist")
    return _xml(root)


@router.get(
    "/resourcesync/capabilitylist.xml",
    response_class=Response,
    responses=_xml_responses(),
    openapi_extra=operation_interface("standard-tool/protocol"),
)
def resourcesync_capability_list(
    request: Request,
    _principal: CatalogReader,
) -> Response:
    root = Element("urlset", {"xmlns": _SITEMAP, "xmlns:rs": _RS})
    for path, capability in (
        ("/resourcesync/resourcelist.xml", "resourcelist"),
        ("/resourcesync/changelist.xml", "changelist"),
    ):
        url = SubElement(root, "url")
        SubElement(url, "loc").text = _url(request, path)
        SubElement(url, "rs:md", {"capability": capability})
    return _xml(root)


@router.get(
    "/resourcesync/resourcelist.xml",
    response_class=Response,
    responses=_xml_responses(),
    openapi_extra=operation_interface("standard-tool/protocol"),
)
def resourcesync_resource_list(
    request: Request,
    principal: CatalogReader,
    container: ContainerDep,
) -> Response:
    pages = container.retrieval.resource_list_pages(
        per_page=_RESOURCE_LIST_PAGE_SIZE,
        principal=principal,
    )
    root = Element("sitemapindex", {"xmlns": _SITEMAP, "xmlns:rs": _RS})
    SubElement(root, "rs:md", {"capability": "resourcelist"})
    for page in range(1, pages + 1):
        sitemap = SubElement(root, "sitemap")
        SubElement(sitemap, "loc").text = _url(
            request,
            f"/resourcesync/resourcelist/{page}.xml",
        )
    return _xml(root)


@router.get(
    "/resourcesync/resourcelist/{page}.xml",
    response_class=Response,
    responses=_xml_responses(),
    openapi_extra=operation_interface("standard-tool/protocol"),
)
def resourcesync_resource_list_page(
    page: Annotated[int, Path(ge=1)],
    request: Request,
    principal: CatalogReader,
    container: ContainerDep,
) -> Response:
    payload = container.retrieval.resource_list_page(
        page=page,
        per_page=_RESOURCE_LIST_PAGE_SIZE,
        principal=principal,
    )
    root = Element("urlset", {"xmlns": _SITEMAP, "xmlns:rs": _RS})
    SubElement(
        root,
        "rs:ln",
        {
            "rel": "index",
            "href": _url(request, "/resourcesync/resourcelist.xml"),
        },
    )
    SubElement(root, "rs:md", {"capability": "resourcelist"})
    for resource in cast(list[dict[str, object]], payload["resources"]):
        url = SubElement(root, "url")
        collection_id = resource["collection_id"]
        SubElement(url, "loc").text = _url(
            request,
            f"/v1/catalog/collections/{collection_id}/manifest",
        )
        SubElement(url, "rs:md", {"hash": f"sha-256:{resource['etag']}"})
    return _xml(root)


@router.get(
    "/resourcesync/changelist.xml",
    response_class=Response,
    responses=_xml_responses(),
    openapi_extra=operation_interface("standard-tool/protocol"),
)
def resourcesync_change_list(
    request: Request,
    principal: CatalogReader,
    container: ContainerDep,
    after: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    payload = container.retrieval.change_list(after=after, principal=principal)
    root = Element("urlset", {"xmlns": _SITEMAP, "xmlns:rs": _RS})
    root.set("data-cursor", str(payload["cursor"]))
    root.set("data-has-more", str(bool(payload["has_more"])).lower())
    for change in cast(list[dict[str, object]], payload["changes"]):
        url = SubElement(root, "url")
        SubElement(url, "loc").text = _url(
            request,
            f"/v1/catalog/collections/{change['collection_id']}/manifest",
        )
        SubElement(
            url,
            "rs:md",
            {
                "change": str(change["change"]),
                "datetime": str(change["occurred_at"]),
                "hash": f"sha-256:{change['etag']}",
            },
        )
    return _xml(root)


@router.get(
    "/v1/catalog/collections/{collection_id}/manifest",
    response_model=None,
    responses={
        200: {
            "description": "OK",
            "content": {"application/json": {"schema": portable_collection_json_schema()}},
            "headers": {
                "ETag": {
                    "description": "Quoted SHA-256 identity of the canonical manifest bytes.",
                    "schema": {"type": "string", "pattern": '^"[0-9a-f]{64}"$'},
                }
            },
        }
    },
    openapi_extra=operation_interface("standard-tool/protocol"),
)
def get_portable_collection_manifest(
    collection_id: int,
    principal: CatalogReader,
    container: ContainerDep,
    response: Response,
) -> PortableCollectionRecord:
    record, etag = container.retrieval.collection_manifest(
        collection_id,
        principal=principal,
    )
    response.headers["ETag"] = f'"{etag}"'
    return record
