from __future__ import annotations

from riverhog_protocol.errors import BadRequest, NotFound
from sqlalchemy import func, literal, select, union_all
from sqlalchemy.orm import Session

from riverhog_core.app_permissions import ARCHIVES_READ, ApplicationPrincipal
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionMetadataPublicationRecord,
)
from riverhog_core.collection_access import collection_access_filter
from riverhog_core.domain.models import (
    ArchiveDownloadAllowance,
    ArchiveStoreListPage,
    ArchiveStoreSummary,
)
from riverhog_core.ports.download_allowance import DownloadAllowance
from riverhog_core.runtime_config import ArchiveStoreConfig, RuntimeConfig
from riverhog_core.services.download_allowances import SqlAlchemyDownloadAllowance

_SORT_FIELDS = {
    "store",
    "backend",
    "storage_class",
    "read_mode",
    "read_priority",
    "collections",
    "objects",
    "stored_bytes",
}


class SqlAlchemyArchiveStoreService:
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        download_allowance: DownloadAllowance | None = None,
    ) -> None:
        self._config = config
        self._session_factory = make_session_factory(config.database_url)
        self._download_allowance = download_allowance or SqlAlchemyDownloadAllowance(config)
        self._read_priorities = {
            name: priority for priority, name in enumerate(config.archive_read_order, start=1)
        }

    def get(
        self,
        store: str,
        *,
        principal: ApplicationPrincipal | None = None,
    ) -> ArchiveStoreSummary:
        normalized = store.strip().casefold()
        config = self._config.archive_stores.get(normalized)
        if config is None:
            raise NotFound(f"archive store not found: {normalized}")
        with session_scope(self._session_factory) as session:
            aggregates = _store_aggregates(
                session,
                stores=(normalized,),
                principal=principal,
            )
        return self._summary(
            config,
            aggregate=aggregates.get(normalized, (0, 0, 0)),
            allowances=self._allowances(),
        )

    def list(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        sort: str,
        order: str,
        all_items: bool = False,
        principal: ApplicationPrincipal | None = None,
    ) -> ArchiveStoreListPage:
        if page < 1:
            raise BadRequest("page must be at least 1")
        if per_page < 1:
            raise BadRequest("per_page must be at least 1")
        if sort not in _SORT_FIELDS:
            raise BadRequest(f"sort must be one of {', '.join(sorted(_SORT_FIELDS))}")
        if order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")

        needle = q.strip().casefold() if q and q.strip() else None
        configs = [
            current
            for current in self._config.archive_stores.values()
            if needle is None
            or needle
            in " ".join(
                (current.name, current.backend, current.storage_class, current.read_mode)
            ).casefold()
        ]
        with session_scope(self._session_factory) as session:
            aggregates = _store_aggregates(
                session,
                stores=tuple(current.name for current in configs),
                principal=principal,
            )
        allowances = self._allowances()
        summaries = [
            self._summary(
                current,
                aggregate=aggregates.get(current.name, (0, 0, 0)),
                allowances=allowances,
            )
            for current in configs
        ]
        summaries.sort(
            key=lambda current: (getattr(current, sort), current.store),
            reverse=order == "desc",
        )
        total = len(summaries)
        pages = (total + per_page - 1) // per_page if total else 0
        if all_items:
            selected = summaries
        else:
            start = (page - 1) * per_page
            selected = summaries[start : start + per_page]
        return ArchiveStoreListPage(
            page=1 if all_items else page,
            per_page=total if all_items else per_page,
            total=total,
            pages=(1 if total else 0) if all_items else pages,
            sort=sort,
            order=order,
            query=needle,
            stores=selected,
        )

    def _allowances(self) -> dict[str, ArchiveDownloadAllowance]:
        return {
            current.store: current for current in self._download_allowance.get_statuses()
        }

    def _summary(
        self,
        config: ArchiveStoreConfig,
        *,
        aggregate: tuple[int, int, int],
        allowances: dict[str, ArchiveDownloadAllowance],
    ) -> ArchiveStoreSummary:
        collections, objects, stored_bytes = aggregate
        return ArchiveStoreSummary(
            store=config.name,
            backend=config.backend,
            storage_class=config.storage_class,
            read_mode=config.read_mode,
            read_priority=self._read_priorities[config.name],
            write_target=config.name == self._config.archive_write_store,
            collections=collections,
            objects=objects,
            stored_bytes=stored_bytes,
            download_allowance=allowances.get(config.name),
        )


def _store_aggregates(
    session: Session,
    *,
    stores: tuple[str, ...],
    principal: ApplicationPrincipal | None,
) -> dict[str, tuple[int, int, int]]:
    if not stores:
        return {}
    copies = (
        select(
            CollectionArchiveCopyRecord.collection_id.label("collection_id"),
            CollectionArchiveCopyRecord.store.label("store"),
        )
        .where(
            CollectionArchiveCopyRecord.state == "uploaded",
            CollectionArchiveCopyRecord.store.in_(stores),
            collection_access_filter(
                CollectionArchiveCopyRecord.collection_id,
                principal,
                ARCHIVES_READ,
            ),
        )
        .subquery()
    )
    immutable = select(
        CollectionArchiveObjectRecord.collection_id.label("collection_id"),
        CollectionArchiveObjectRecord.store.label("store"),
        literal(1).label("objects"),
        CollectionArchiveObjectRecord.stored_bytes.label("stored_bytes"),
    )
    mutable = select(
        CollectionMetadataPublicationRecord.collection_id.label("collection_id"),
        CollectionMetadataPublicationRecord.store.label("store"),
        literal(1).label("objects"),
        func.coalesce(CollectionMetadataPublicationRecord.stored_bytes, 0).label(
            "stored_bytes"
        ),
    ).where(CollectionMetadataPublicationRecord.object_path.is_not(None))
    owned = union_all(immutable, mutable).subquery()
    rows = session.execute(
        select(
            copies.c.store,
            func.count(func.distinct(copies.c.collection_id)),
            func.coalesce(func.sum(owned.c.objects), 0),
            func.coalesce(func.sum(owned.c.stored_bytes), 0),
        )
        .outerjoin(
            owned,
            (owned.c.collection_id == copies.c.collection_id)
            & (owned.c.store == copies.c.store),
        )
        .group_by(copies.c.store)
    )
    return {
        str(store): (int(collections), int(objects), int(stored_bytes))
        for store, collections, objects, stored_bytes in rows
    }
