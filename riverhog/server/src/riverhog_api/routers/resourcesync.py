from __future__ import annotations

from typing import cast
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import APIRouter, Query, Request, Response

from riverhog_api.auth import CatalogReader
from riverhog_api.deps import ContainerDep

router = APIRouter(tags=["catalog"])
_RS = "http://www.openarchives.org/rs/terms/"
_SITEMAP = "http://www.sitemaps.org/schemas/sitemap/0.9"


def _url(request: Request, path: str) -> str:
    return str(request.base_url).rstrip("/") + path


def _xml(root: Element) -> Response:
    return Response(
        content=tostring(root, encoding="utf-8", xml_declaration=True),
        media_type="application/xml",
    )


@router.get("/.well-known/resourcesync")
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


@router.get("/resourcesync/capabilitylist.xml")
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


@router.get("/resourcesync/resourcelist.xml")
def resourcesync_resource_list(
    request: Request,
    principal: CatalogReader,
    container: ContainerDep,
) -> Response:
    root = Element("urlset", {"xmlns": _SITEMAP, "xmlns:rs": _RS})
    for resource in container.retrieval.resource_list(principal=principal):
        url = SubElement(root, "url")
        collection_id = resource["collection_id"]
        SubElement(url, "loc").text = _url(
            request,
            f"/v1/catalog/collections/{collection_id}/manifest",
        )
        SubElement(url, "rs:md", {"hash": f"sha-256:{resource['etag']}"})
    return _xml(root)


@router.get("/resourcesync/changelist.xml")
def resourcesync_change_list(
    request: Request,
    principal: CatalogReader,
    container: ContainerDep,
    after: int = Query(0, ge=0),
) -> Response:
    payload = container.retrieval.change_list(after=after, principal=principal)
    root = Element("urlset", {"xmlns": _SITEMAP, "xmlns:rs": _RS})
    root.set("data-cursor", str(payload["cursor"]))
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


@router.get("/v1/catalog/collections/{collection_id:path}/manifest")
def collection_portable_manifest(
    collection_id: str,
    principal: CatalogReader,
    container: ContainerDep,
) -> Response:
    payload, etag = container.retrieval.collection_manifest(
        collection_id,
        principal=principal,
    )
    import json

    return Response(
        content=json.dumps(payload, sort_keys=True, separators=(",", ":")),
        media_type="application/json",
        headers={"ETag": f'"{etag}"'},
    )
