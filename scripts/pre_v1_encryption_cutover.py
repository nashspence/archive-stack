#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from contextlib import ExitStack

from riverhog_core.catalog_db import dispose_session_factory, make_session_factory, validate_db
from riverhog_core.pre_v1_encryption_cutover import PreV1EncryptionCutover
from riverhog_core.runtime_config import StorageAdapterRegistration, load_runtime_config
from riverhog_core.stores.storage_adapter_archive_objects import (
    StorageAdapterImmutableArchiveObjectStore,
)
from riverhog_storage_adapter_support import StorageAdapterClient


def _adapter_client(registration: StorageAdapterRegistration) -> StorageAdapterClient:
    return StorageAdapterClient.from_token_file(
        registration.base_url,
        token_file=registration.token_file,
        allow_insecure_http=registration.allow_insecure_http,
        timeout=registration.timeout_seconds,
        maximum_connections=registration.maximum_connections,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan or execute the one-off pre-v1 archive encryption descriptor cutover."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="publish and catalog descriptors; the default is a read-only plan",
    )
    args = parser.parse_args()
    config = load_runtime_config()
    session_factory = make_session_factory(config.database_url)
    try:
        validate_db(config.database_url)
        if not args.execute:
            items = PreV1EncryptionCutover(session_factory=session_factory).plan()
        else:
            with ExitStack() as cleanup:
                stores = {}
                for name, registration in config.archive_stores.items():
                    client = _adapter_client(registration)
                    cleanup.callback(client.close)
                    client.check_readiness()
                    stores[name] = StorageAdapterImmutableArchiveObjectStore(client)
                items = PreV1EncryptionCutover(
                    session_factory=session_factory,
                    immutable_stores=stores,
                ).execute()
        print(
            json.dumps(
                {
                    "format": "riverhog-pre-v1-encryption-cutover/v1",
                    "mode": "execute" if args.execute else "plan",
                    "copies": [
                        {
                            "collection": item.collection_id,
                            "store": item.store,
                            "descriptor_path": item.descriptor_path,
                        }
                        for item in items
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    finally:
        dispose_session_factory(session_factory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
