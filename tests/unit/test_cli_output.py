from __future__ import annotations

from arc_cli.output import (
    format_archive_status,
    format_collection_summary,
    format_glacier_report,
    format_images,
)


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
                    "physical_copies_verified": 0,
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
    assert "verified=0/2" in rendered
    assert "glacier_failure: s3 timeout" in rendered


def test_format_archive_status_surfaces_pending_finalization_and_protected_collections() -> None:
    rendered = format_archive_status(
        {
            "candidates": [
                {
                    "candidate_id": "img_2026-04-20_01",
                    "fill": 0.84,
                    "collections": 1,
                    "collection_ids": ["docs"],
                }
            ]
        },
        {
            "page": 1,
            "per_page": 25,
            "images": [
                {
                    "id": "20260420T040001Z",
                    "filename": "20260420T040001Z.iso",
                    "collections": 1,
                    "collection_ids": ["docs"],
                    "protection_state": "partially_protected",
                    "physical_copies_registered": 1,
                    "physical_copies_verified": 0,
                    "physical_copies_required": 2,
                    "glacier": {"state": "pending"},
                }
            ],
        },
        {
            "collections": [
                {
                    "id": "receipts",
                    "bytes": 40,
                    "protected_bytes": 40,
                }
            ]
        },
    )
    assert "waiting_finalization:" in rendered
    assert "img_2026-04-20_01" in rendered
    assert "next: burn, verify, glacier=pending" in rendered
    assert "fully_protected_collections:" in rendered
    assert "receipts protected_bytes=40/40" in rendered


def test_format_collection_summary_surfaces_paths_labels_and_glacier_costs() -> None:
    rendered = format_collection_summary(
        {
            "id": "docs",
            "files": 2,
            "bytes": 33,
            "hot_bytes": 0,
            "archived_bytes": 33,
            "pending_bytes": 0,
            "protection_state": "partially_protected",
            "protected_bytes": 0,
            "image_coverage": [
                {
                    "id": "20260420T040001Z",
                    "filename": "20260420T040001Z.iso",
                    "protection_state": "partially_protected",
                    "physical_copies_registered": 1,
                    "physical_copies_verified": 1,
                    "physical_copies_required": 2,
                    "glacier": {
                        "state": "uploaded",
                        "object_path": (
                            "glacier/finalized-images/20260420T040001Z/"
                            "20260420T040001Z.iso"
                        ),
                    },
                    "covered_paths": ["tax/2022/invoice-123.pdf"],
                    "copies": [
                        {
                            "id": "20260420T040001Z-1",
                            "label_text": "20260420T040001Z-1",
                            "location": "Shelf B1",
                            "state": "verified",
                            "verification_state": "verified",
                        }
                    ],
                }
            ],
        },
        {
            "collections": [
                {
                    "id": "docs",
                    "represented_bytes": 33,
                    "derived_stored_bytes": 8200,
                    "estimated_monthly_cost_usd": 0.000192,
                    "images": [
                        {
                            "image_id": "20260420T040001Z",
                            "represented_bytes": 33,
                            "derived_stored_bytes": 8200,
                            "estimated_monthly_cost_usd": 0.000192,
                        }
                    ],
                }
            ]
        },
    )
    assert "glacier_footprint: represented_bytes=33 derived_stored_bytes=8200" in rendered
    assert "paths: tax/2022/invoice-123.pdf" in rendered
    assert "label=20260420T040001Z-1" in rendered
    assert (
        "glacier_path: glacier/finalized-images/20260420T040001Z/20260420T040001Z.iso"
        in rendered
    )


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
                "actuals": {
                    "source": "aws_cost_explorer_resource",
                    "scope": "bucket",
                    "filter_label": "riverhog",
                    "billing_view_arn": "arn:aws:billing::123456789012:billingview/primary",
                    "granularity": "DAILY",
                    "periods": [
                        {
                            "start": "2026-04-14",
                            "end": "2026-04-15",
                            "estimated": False,
                            "unblended_cost_usd": 0.44,
                            "usage_quantity": 11.0,
                            "usage_unit": "N/A",
                        }
                    ],
                    "notes": [],
                },
                "forecast": {
                    "source": "aws_cost_explorer",
                    "scope": "tag",
                    "filter_label": "backup_set=optical_archive",
                    "granularity": "MONTHLY",
                    "periods": [
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
                "exports": {
                    "source": "aws_data_exports_s3",
                    "scope": "bucket",
                    "filter_label": "riverhog",
                    "export_arn": "arn:aws:bcm-data-exports:us-east-1:123456789012:export/glacier",
                    "export_name": "glacier-export",
                    "execution_id": "execution-0002",
                    "manifest_key": "billing/glacier-export/metadata/execution-0002/manifest.json",
                    "billing_period": "2026-04-01..2026-05-01",
                    "object_key": None,
                    "files_read": 2,
                    "breakdowns": [
                        {
                            "usage_type": "TimedStorage-GlacierByteHrs",
                            "operation": "StandardStorage",
                            "resource_id": "riverhog",
                            "tag_value": None,
                            "unblended_cost_usd": 1.25,
                        }
                    ],
                    "notes": [],
                },
                "invoices": {
                    "source": "aws_invoicing",
                    "scope": "account",
                    "account_id": "123456789012",
                    "invoices": [
                        {
                            "invoice_id": "INV-001",
                            "billing_period_start": "2026-04-01",
                            "billing_period_end": "2026-05-01",
                            "base_total_amount": 99.5,
                            "payment_total_amount": 99.5,
                        }
                    ],
                    "notes": [],
                },
                "notes": [],
            },
            "history": [],
        }
    )
    assert "pricing_basis: aws-s3-us-west-2-public" in rendered
    assert "source=manual" in rendered
    assert "region=us-west-2" in rendered
    assert "billing:" in rendered
    assert "source=aws_cost_explorer_resource scope=bucket" in rendered
    assert "billing_view_arn: arn:aws:billing::123456789012:billingview/primary" in rendered
    assert "source=aws_data_exports_s3 scope=bucket" in rendered
    assert "export_name: glacier-export" in rendered
    assert "execution_id: execution-0002" in rendered
    assert "manifest_key: billing/glacier-export/metadata/execution-0002/manifest.json" in rendered
    assert "billing_period: 2026-04-01..2026-05-01" in rendered
    assert "files_read: 2" in rendered
    assert "source=aws_invoicing scope=account" in rendered
    assert "period: 2026-05-01..2026-06-01 mean_cost_usd=14.5" in rendered
    assert "attribution=derived" in rendered
    assert "estimated_monthly_cost_usd=0.000192" in rendered
