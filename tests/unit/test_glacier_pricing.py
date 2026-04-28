from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from arc_core.runtime_config import RuntimeConfig
from arc_core.services.glacier_pricing import resolve_glacier_pricing


def _config(tmp_path: Path, **overrides: object) -> RuntimeConfig:
    config = RuntimeConfig(
        object_store="s3",
        s3_endpoint_url="http://example.invalid:9000",
        s3_region="us-east-1",
        s3_bucket="riverhog",
        s3_access_key_id="test-access",
        s3_secret_access_key="test-secret",
        s3_force_path_style=True,
        tusd_base_url="http://example.invalid:1080/files",
        tusd_hook_secret="hook-secret",
        sqlite_path=tmp_path / "state.sqlite3",
    )
    return replace(config, **overrides)


def test_resolve_glacier_pricing_returns_manual_basis_for_non_aws_endpoint(
    tmp_path: Path,
) -> None:
    basis = resolve_glacier_pricing(_config(tmp_path))

    assert basis.source == "manual"
    assert basis.region_code == "us-west-2"
    assert basis.currency_code == "USD"
    assert basis.glacier_storage_rate_usd_per_gib_month == 0.00099
    assert basis.standard_storage_rate_usd_per_gib_month == 0.023


def test_resolve_glacier_pricing_uses_bulk_api_rates_when_available(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _config(
        tmp_path,
        glacier_endpoint_url="",
        glacier_pricing_cache_ttl=timedelta(seconds=0),
    )
    price_list_document = {
        "products": {
            "standard-sku": {
                "productFamily": "Storage",
                "attributes": {
                    "regionCode": "us-west-2",
                    "storageClass": "General Purpose",
                    "volumeType": "Standard",
                    "usagetype": "USW2-TimedStorage-ByteHrs",
                },
            },
            "deep-archive-sku": {
                "productFamily": "Storage",
                "attributes": {
                    "regionCode": "us-west-2",
                    "storageClass": "Intelligent-Tiering",
                    "volumeType": "IntelligentTieringDeepArchiveAccess",
                    "usagetype": "USW2-TimedStorage-INT-DAA-ByteHrs",
                },
            },
        },
        "terms": {
            "OnDemand": {
                "standard-sku": {
                    "standard-term": {
                        "effectiveDate": "2026-04-01T00:00:00Z",
                        "priceDimensions": {
                            "standard-dimension": {
                                "beginRange": "0",
                                "endRange": "51200",
                                "unit": "GB-Mo",
                                "pricePerUnit": {"USD": "0.0230000000"},
                                "description": (
                                    "$0.023 per GB - first 50 TB / month of storage used"
                                ),
                            }
                        },
                    }
                },
                "deep-archive-sku": {
                    "deep-archive-term": {
                        "effectiveDate": "2026-04-01T00:00:00Z",
                        "priceDimensions": {
                            "deep-archive-dimension": {
                                "beginRange": "0",
                                "endRange": "Inf",
                                "unit": "GB-Mo",
                                "pricePerUnit": {"USD": "0.0009900000"},
                                "description": (
                                    "$0.00099 per Gigabyte Month for "
                                    "TimedStorage-INT-DAA-ByteHrs:IntelligentTieringDAAStorage"
                                ),
                            }
                        },
                    }
                },
            }
        },
    }

    class _FakePricingClient:
        def list_price_lists(self, **kwargs):
            assert kwargs["ServiceCode"] == "AmazonS3"
            assert kwargs["CurrencyCode"] == "USD"
            assert kwargs["RegionCode"] == "us-west-2"
            return {
                "PriceLists": [
                    {
                        "PriceListArn": (
                            "arn:aws:pricing:::price-list/aws/AmazonS3/USD/"
                            "20260427212459/us-west-2"
                        ),
                        "RegionCode": "us-west-2",
                        "FileFormats": ["json"],
                    }
                ]
            }

        def get_price_list_file_url(self, **kwargs):
            assert kwargs["FileFormat"] == "json"
            return {"Url": "https://pricing.example.invalid/AmazonS3/us-west-2/index.json"}

    monkeypatch.setattr(
        "arc_core.services.glacier_pricing._create_pricing_client",
        lambda current: _FakePricingClient(),
    )
    monkeypatch.setattr(
        "arc_core.services.glacier_pricing._download_price_list_json",
        lambda url: price_list_document,
    )

    basis = resolve_glacier_pricing(config)

    assert basis.source == "aws_price_list_bulk_api"
    assert basis.label == "aws-price-list-bulk-api:USD:us-west-2"
    assert basis.region_code == "us-west-2"
    assert basis.currency_code == "USD"
    assert basis.effective_at == "2026-04-01T00:00:00Z"
    assert basis.price_list_arn == (
        "arn:aws:pricing:::price-list/aws/AmazonS3/USD/20260427212459/us-west-2"
    )
    assert basis.glacier_storage_rate_usd_per_gib_month == 0.00099
    assert basis.standard_storage_rate_usd_per_gib_month == 0.023


def test_resolve_glacier_pricing_falls_back_when_auto_lookup_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _config(
        tmp_path,
        glacier_endpoint_url="",
        glacier_pricing_cache_ttl=timedelta(seconds=0),
    )
    monkeypatch.setattr(
        "arc_core.services.glacier_pricing._create_pricing_client",
        lambda current: (_ for _ in ()).throw(RuntimeError("pricing unavailable")),
    )

    basis = resolve_glacier_pricing(config)

    assert basis.source == "manual_fallback"
    assert basis.effective_at is None
    assert basis.price_list_arn is None
    assert basis.glacier_storage_rate_usd_per_gib_month == 0.00099
    assert basis.standard_storage_rate_usd_per_gib_month == 0.023
