from __future__ import annotations

from arc_cli.output import format_glacier_report, format_images


def test_format_images_surfaces_glacier_failure_context() -> None:
    rendered = format_images(
        {
            "page": 1,
            "pages": 1,
            "per_page": 25,
            "total": 1,
            "sort": "finalized_at",
            "order": "desc",
            "images": [
                {
                    "id": "20260420T040001Z",
                    "filename": "20260420T040001Z.iso",
                    "finalized_at": "2026-04-20T04:00:01Z",
                    "collections": 1,
                    "collection_ids": ["docs"],
                    "protection_state": "partially_protected",
                    "physical_copies_registered": 1,
                    "physical_copies_required": 2,
                    "glacier": {
                        "state": "failed",
                        "object_path": None,
                        "failure": "s3 timeout",
                    },
                }
            ],
        }
    )
    assert "glacier=failed" in rendered
    assert "glacier_failure: s3 timeout" in rendered


def test_format_glacier_report_surfaces_pricing_basis_and_collection_derivation() -> None:
    rendered = format_glacier_report(
        {
            "scope": "collection",
            "measured_at": "2026-04-28T00:00:00Z",
            "pricing_basis": {
                "label": "aws-s3-us-west-2-public",
                "source": "manual",
                "storage_class": "DEEP_ARCHIVE",
                "region_code": "us-west-2",
                "effective_at": None,
                "glacier_storage_rate_usd_per_gib_month": 0.00099,
                "standard_storage_rate_usd_per_gib_month": 0.023,
                "archived_metadata_bytes_per_object": 32768,
                "standard_metadata_bytes_per_object": 8192,
                "minimum_storage_duration_days": 180,
            },
            "totals": {
                "images": 1,
                "uploaded_images": 1,
                "measured_storage_bytes": 8200,
                "estimated_billable_bytes": 49160,
                "estimated_monthly_cost_usd": 0.000192,
            },
            "images": [
                {
                    "id": "20260420T040001Z",
                    "filename": "20260420T040001Z.iso",
                    "measured_storage_bytes": 8200,
                    "estimated_billable_bytes": 49160,
                    "estimated_monthly_cost_usd": 0.000192,
                    "glacier": {
                        "state": "uploaded",
                        "object_path": (
                            "glacier/finalized-images/20260420T040001Z/"
                            "20260420T040001Z.iso"
                        ),
                    },
                }
            ],
            "collections": [
                {
                    "id": "docs",
                    "attribution_state": "derived",
                    "represented_bytes": 33,
                    "derived_stored_bytes": 8200,
                    "estimated_monthly_cost_usd": 0.000192,
                }
            ],
            "billing": {
                "source": "aws_cost_explorer",
                "scope": "tag",
                "filter_label": "backup_set=optical_archive",
                "actuals": [
                    {
                        "start": "2026-04-01",
                        "end": "2026-05-01",
                        "estimated": True,
                        "unblended_cost_usd": 12.34,
                        "usage_quantity": 56.78,
                        "usage_unit": "N/A",
                    }
                ],
                "forecast": [
                    {
                        "start": "2026-05-01",
                        "end": "2026-06-01",
                        "mean_cost_usd": 14.5,
                        "lower_bound_cost_usd": 11.0,
                        "upper_bound_cost_usd": 18.0,
                    }
                ],
                "notes": [],
            },
            "history": [],
        }
    )
    assert "pricing_basis: aws-s3-us-west-2-public" in rendered
    assert "source=manual" in rendered
    assert "region=us-west-2" in rendered
    assert "billing:" in rendered
    assert "source=aws_cost_explorer scope=tag" in rendered
    assert "forecast: 2026-05-01..2026-06-01 mean_cost_usd=14.5" in rendered
    assert "attribution=derived" in rendered
    assert "estimated_monthly_cost_usd=0.000192" in rendered
