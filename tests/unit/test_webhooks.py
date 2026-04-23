from __future__ import annotations

from datetime import UTC, datetime

from arc_core.webhooks import (
    ImagesReadyBatch,
    ReadyImage,
    WebhookConfig,
    build_images_ready_payload,
)


def test_build_images_ready_payload_supports_multiple_images() -> None:
    payload = build_images_ready_payload(
        config=WebhookConfig(url="https://example.test/hook", base_url="https://api.test"),
        batch=ImagesReadyBatch(
            batch_id="batch-1",
            images=[
                ReadyImage(
                    image_id="20260420T040001Z", filename="20260420T040001Z.iso", iso_available=True
                ),
                ReadyImage(
                    image_id="20260420T040002Z", filename="20260420T040002Z.iso", iso_available=True
                ),
            ],
        ),
        delivered_at=datetime(2026, 4, 20, tzinfo=UTC),
    )
    assert payload["event"] == "images.ready"
    assert len(payload["images"]) == 2
    assert payload["images"][0]["download_url"].endswith("/v1/images/20260420T040001Z/iso")
