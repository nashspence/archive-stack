from __future__ import annotations

import logging
from pathlib import Path

from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.notification_routing import post_collection_webhooks


def test_collection_lifecycle_webhooks_use_default_recipient_routes(tmp_path: Path) -> None:
    config = RuntimeConfig(
        database_url=f"sqlite:///{tmp_path / 'riverhog.db'}",
        public_base_url="https://riverhog.example.test",
        operator_webhook_url="https://operations.example.test/hook",
        collection_webhook_urls={
            "operator": "https://collections.example.test/operator",
        },
        collection_webhook_default_recipients=("operator",),
    )
    deliveries: list[tuple[str, dict[str, object]]] = []

    def capture(*, config, payload):  # type: ignore[no-untyped-def]
        deliveries.append((config.url, payload))

    post_collection_webhooks(
        config,
        event="collections.upload_staged",
        collection_id="2026/2026-07-16T12:00:00Z__example",
        details={
            "bytes_total": 10,
            "files_total": 1,
            "files_uploaded": 1,
            "state": "archiving",
            "uploaded_bytes": 10,
        },
        post=capture,
        log=logging.getLogger(__name__),
    )

    assert [(url, payload["recipient"]) for url, payload in deliveries] == [
        ("https://collections.example.test/operator", "operator")
    ]
