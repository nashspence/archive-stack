from __future__ import annotations

from tests.harness import configure_garage


class _FakeS3Client:
    def __init__(self) -> None:
        self.put_buckets: list[str] = []
        self.lifecycle_by_bucket: dict[str, dict[str, object]] = {}

    def put_bucket_lifecycle_configuration(
        self,
        *,
        Bucket: str,
        LifecycleConfiguration: dict[str, object],
    ) -> None:
        self.put_buckets.append(Bucket)
        self.lifecycle_by_bucket[Bucket] = LifecycleConfiguration

    def get_bucket_lifecycle_configuration(self, *, Bucket: str) -> dict[str, object]:
        return self.lifecycle_by_bucket[Bucket]


def test_lifecycle_targets_cover_each_distinct_test_adapter_bucket(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    clients = [_FakeS3Client(), _FakeS3Client()]
    monkeypatch.setattr(configure_garage, "create_s3_client", lambda _config: clients.pop(0))

    targets = configure_garage._lifecycle_targets(
        {
            "RIVERHOG_GARAGE_ARCHIVE_BUCKET": "riverhog-archive",
            "RIVERHOG_GARAGE_CACHE_BUCKET": "riverhog-retrieval-cache",
        }
    )

    assert [bucket for _client, bucket in targets] == [
        "riverhog-archive",
        "riverhog-retrieval-cache",
    ]


def test_configure_bucket_lifecycle_verifies_expected_payload() -> None:
    client = _FakeS3Client()

    configure_garage._configure_bucket_lifecycle(client=client, bucket="riverhog-archive")

    assert client.put_buckets == ["riverhog-archive"]
    assert client.lifecycle_by_bucket["riverhog-archive"] == (
        configure_garage.EXPECTED_LIFECYCLE_CONFIGURATION
    )
