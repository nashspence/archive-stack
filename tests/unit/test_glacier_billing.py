from __future__ import annotations

import gzip
import io
import json
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.glacier_billing import resolve_glacier_billing


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


def test_resolve_glacier_billing_returns_unavailable_for_non_aws_runtime(tmp_path: Path) -> None:
    summary = resolve_glacier_billing(_config(tmp_path), include=True)

    assert summary is not None
    assert summary.actuals is not None
    assert summary.actuals.source == "unavailable"
    assert summary.forecast is not None
    assert summary.forecast.source == "unavailable"
    assert summary.exports is not None
    assert summary.exports.source == "unavailable"
    assert summary.invoices is not None
    assert summary.invoices.source == "unavailable"


def test_resolve_glacier_billing_prefers_bucket_scoped_actuals(
    tmp_path: Path,
    monkeypatch,
) -> None:
    recorded: dict[str, object] = {}

    class _FakeCostExplorerClient:
        def get_cost_and_usage_with_resources(self, **kwargs):
            calls = recorded.setdefault("resource_calls", [])
            assert isinstance(calls, list)
            calls.append(kwargs)
            return {
                "ResultsByTime": [
                    {
                        "TimePeriod": {"Start": "2026-04-14", "End": "2026-04-15"},
                        "Estimated": False,
                        "Groups": [
                            {
                                "Keys": ["arn:aws:s3:::riverhog"],
                                "Metrics": {
                                    "UnblendedCost": {"Amount": "0.44", "Unit": "USD"},
                                    "UsageQuantity": {"Amount": "11.00", "Unit": "GB-Mo"},
                                },
                            },
                            {
                                "Keys": ["arn:aws:s3:::other-bucket"],
                                "Metrics": {
                                    "UnblendedCost": {"Amount": "9.99", "Unit": "USD"},
                                    "UsageQuantity": {"Amount": "900.00", "Unit": "GB-Mo"},
                                },
                            },
                        ],
                    }
                ]
            }

        def get_cost_and_usage(self, **kwargs):
            raise AssertionError("fallback actuals should not run when bucket actuals succeed")

        def get_cost_forecast(self, **kwargs):
            recorded["forecast"] = kwargs
            return {
                "ForecastResultsByTime": [
                    {
                        "TimePeriod": {"Start": "2026-05-01", "End": "2026-06-01"},
                        "MeanValue": "14.50",
                        "PredictionIntervalLowerBound": "11.00",
                        "PredictionIntervalUpperBound": "18.00",
                    }
                ]
            }

    class _FakeBillingClient:
        def list_billing_views(self, **kwargs):
            recorded["billing_views"] = kwargs
            return {
                "billingViews": [
                    {"arn": "arn:aws:billing::123456789012:billingview/primary"}
                ]
            }

    monkeypatch.setattr(
        "riverhog_core.services.glacier_billing._create_cost_explorer_client",
        lambda config: _FakeCostExplorerClient(),
    )
    monkeypatch.setattr(
        "riverhog_core.services.glacier_billing._create_billing_client",
        lambda config: _FakeBillingClient(),
    )
    monkeypatch.setattr(
        "riverhog_core.services.glacier_billing._resolve_billing_exports",
        lambda config: None,
    )
    monkeypatch.setattr(
        "riverhog_core.services.glacier_billing._resolve_invoices",
        lambda config: None,
    )

    summary = resolve_glacier_billing(
        _config(
            tmp_path,
            glacier_endpoint_url="https://s3.us-west-2.amazonaws.com",
            glacier_backend="aws",
        ),
        include=True,
    )

    assert summary is not None
    assert summary.actuals is not None
    assert summary.actuals.source == "aws_cost_explorer_resource"
    assert summary.actuals.scope == "bucket"
    assert summary.actuals.filter_label == "riverhog"
    assert (
        summary.actuals.billing_view_arn
        == "arn:aws:billing::123456789012:billingview/primary"
    )
    assert summary.actuals.granularity == "DAILY"
    assert summary.actuals.periods[0].unblended_cost_usd == 0.44
    assert summary.actuals.periods[0].usage_quantity == 11.0
    assert summary.actuals.periods[0].usage_unit == "GB-Mo"
    assert summary.forecast is not None
    assert summary.forecast.scope == "service"
    assert "bucket-resource forecasting" in summary.forecast.notes[0]

    resource_calls = recorded["resource_calls"]
    assert isinstance(resource_calls, list)
    bucket_filter = resource_calls[0]["Filter"]
    assert bucket_filter == {
        "And": [
            {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Simple Storage Service"]}},
            {"Dimensions": {"Key": "REGION", "Values": ["us-west-2"]}},
        ]
    }
    assert resource_calls[0]["GroupBy"] == [{"Type": "DIMENSION", "Key": "RESOURCE_ID"}]
    assert (
        resource_calls[0]["BillingViewArn"]
        == "arn:aws:billing::123456789012:billingview/primary"
    )
    assert recorded["billing_views"] == {
        "billingViewTypes": ["PRIMARY"],
        "maxResults": 10,
    }


def test_resolve_glacier_billing_falls_back_to_tag_scope_when_bucket_actuals_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class _FakeCostExplorerClient:
        def get_cost_and_usage_with_resources(self, **kwargs):
            raise RuntimeError(
                "DataUnavailableException: resource-level daily granular data unavailable"
            )

        def get_cost_and_usage(self, **kwargs):
            return {
                "ResultsByTime": [
                    {
                        "TimePeriod": {"Start": "2026-04-01", "End": "2026-05-01"},
                        "Estimated": False,
                        "Total": {
                            "UnblendedCost": {"Amount": "12.34", "Unit": "USD"},
                            "UsageQuantity": {"Amount": "56.78", "Unit": "N/A"},
                        },
                    }
                ]
            }

        def get_cost_forecast(self, **kwargs):
            return {"ForecastResultsByTime": []}

    monkeypatch.setattr(
        "riverhog_core.services.glacier_billing._create_cost_explorer_client",
        lambda config: _FakeCostExplorerClient(),
    )
    monkeypatch.setattr(
        "riverhog_core.services.glacier_billing._resolve_billing_exports",
        lambda config: None,
    )
    monkeypatch.setattr(
        "riverhog_core.services.glacier_billing._resolve_invoices",
        lambda config: None,
    )

    summary = resolve_glacier_billing(
        _config(
            tmp_path,
            glacier_endpoint_url="https://s3.us-west-2.amazonaws.com",
            glacier_billing_tag_key="backup_set",
            glacier_billing_tag_value="optical_archive",
        ),
        include=True,
    )

    assert summary is not None
    assert summary.actuals is not None
    assert summary.actuals.source == "aws_cost_explorer"
    assert summary.actuals.scope == "tag"
    assert summary.actuals.filter_label == "backup_set=optical_archive"
    assert "resource-level daily granular data" in summary.actuals.notes[0]


def test_resolve_glacier_billing_reads_cur_or_data_exports_breakdowns(
    tmp_path: Path,
    monkeypatch,
) -> None:
    export_manifest = json.dumps(
        {
            "reportKeys": ["billing/20260401-20260501/report-0001.csv.gz"],
        }
    )
    export_csv = "\n".join(
        [
            "line_item_product_code,line_item_usage_type,line_item_operation,"
            "line_item_resource_id,line_item_unblended_cost,line_item_usage_amount,"
            "line_item_usage_unit",
            "AmazonS3,TimedStorage-GlacierByteHrs,StandardStorage,riverhog,1.25,100,GB-Mo",
            "AmazonS3,Requests-Tier1,GetObject,riverhog,0.25,10,Requests",
        ]
    )

    class _Body:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

    class _Paginator:
        def paginate(self, **kwargs):
            return [
                {
                    "Contents": [
                        {
                            "Key": "billing/20260401-20260501/cur-manifest.json",
                            "LastModified": datetime(2026, 4, 28, tzinfo=UTC),
                        }
                    ]
                }
            ]

    class _FakeS3Client:
        def get_paginator(self, name: str):
            assert name == "list_objects_v2"
            return _Paginator()

        def get_object(self, **kwargs):
            key = kwargs["Key"]
            if key == "billing/20260401-20260501/cur-manifest.json":
                return {"Body": _Body(export_manifest.encode("utf-8"))}
            assert key == "billing/20260401-20260501/report-0001.csv.gz"
            return {"Body": _Body(gzip.compress(export_csv.encode("utf-8")))}

    monkeypatch.setattr(
        "riverhog_core.services.glacier_billing._create_billing_s3_client",
        lambda config: _FakeS3Client(),
    )

    view = resolve_glacier_billing(
        _config(
            tmp_path,
            glacier_endpoint_url="https://s3.us-west-2.amazonaws.com",
            glacier_backend="aws",
            glacier_billing_export_bucket="billing-bucket",
            glacier_billing_export_prefix="billing",
        ),
        include=True,
    ).exports

    assert view is not None
    assert view.source == "aws_cur_s3"
    assert view.scope == "bucket"
    assert view.export_name == "20260401-20260501"
    assert view.bucket == "billing-bucket"
    assert view.manifest_key == "billing/20260401-20260501/cur-manifest.json"
    assert view.billing_period == "20260401-20260501"
    assert view.object_key == "billing/20260401-20260501/report-0001.csv.gz"
    assert view.files_read == 1
    assert view.rows_scanned == 2
    assert view.breakdowns[0].unblended_cost_usd == 1.25
    assert view.breakdowns[0].resource_id == "riverhog"
    assert "latest manifest under the configured billing export prefix" in view.notes[0]


def test_resolve_glacier_billing_reads_data_exports_execution_manifests(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest_key = "billing/glacier-export/metadata/execution-0002/manifest.json"
    manifest = json.dumps(
        {
            "billingPeriod": {"start": "2026-04-01", "end": "2026-05-01"},
            "reportFiles": [
                {"filePath": "part-0001.csv.gz"},
                {
                    "s3Key": (
                        "billing/glacier-export/data/BILLING_PERIOD=2026-04/part-0002.zip"
                    )
                },
            ],
        }
    ).encode("utf-8")
    file_one = "\n".join(
        [
            "service,usage_type,operation,resource_id,unblended_cost,usage_quantity,usage_unit",
            (
                "Amazon Simple Storage Service,TimedStorage-GlacierByteHrs,"
                "StandardStorage,arn:aws:s3:::riverhog,1.25,100,GB-Mo"
            ),
            (
                "Amazon Simple Storage Service,Requests-Tier1,GetObject,"
                "arn:aws:s3:::other,0.50,10,Requests"
            ),
        ]
    ).encode("utf-8")
    file_two_csv = "\n".join(
        [
            "service,usage_type,operation,resource_id,unblended_cost,usage_quantity,usage_unit",
            (
                "Amazon Simple Storage Service,TimedStorage-GlacierByteHrs,"
                "StandardStorage,arn:aws:s3:::riverhog,0.75,50,GB-Mo"
            ),
            (
                "Amazon Simple Storage Service,Requests-Tier1,GetObject,"
                "riverhog,0.25,10,Requests"
            ),
        ]
    ).encode("utf-8")
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("part-0002.csv", file_two_csv)
    objects = {
        manifest_key: manifest,
        "billing/glacier-export/metadata/execution-0002/part-0001.csv.gz": gzip.compress(
            file_one
        ),
        "billing/glacier-export/data/BILLING_PERIOD=2026-04/part-0002.zip": (
            archive_buffer.getvalue()
        ),
    }

    class _Body:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

    class _Paginator:
        def paginate(self, **kwargs):
            assert kwargs["Bucket"] == "billing-bucket"
            if kwargs["Prefix"] == "billing/glacier-export/metadata":
                return [
                    {
                        "Contents": [
                            {
                                "Key": (
                                    "billing/glacier-export/metadata/"
                                    "execution-0001/manifest.json"
                                ),
                                "LastModified": datetime(2026, 4, 27, 9, 0, tzinfo=UTC),
                            },
                            {
                                "Key": manifest_key,
                                "LastModified": datetime(2026, 4, 28, 9, 0, tzinfo=UTC),
                            },
                        ]
                    }
                ]
            return [{"Contents": []}]

    class _FakeS3Client:
        def get_paginator(self, name: str):
            assert name == "list_objects_v2"
            return _Paginator()

        def get_object(self, **kwargs):
            return {"Body": _Body(objects[kwargs["Key"]])}

    class _FakeDataExportsClient:
        def list_executions(self, **kwargs):
            assert kwargs == {
                "ExportArn": "arn:aws:bcm-data-exports:us-east-1:123456789012:export/glacier",
                "MaxResults": 20,
            }
            return {
                "Executions": [
                    {
                        "ExecutionId": "execution-0001",
                        "ExecutionStatus": {
                            "StatusCode": "DELIVERY_SUCCESS",
                            "CompletedAt": datetime(2026, 4, 27, 8, 0, tzinfo=UTC),
                        },
                    },
                    {
                        "ExecutionId": "execution-0002",
                        "ExecutionStatus": {
                            "StatusCode": "DELIVERY_SUCCESS",
                            "CompletedAt": datetime(2026, 4, 28, 8, 0, tzinfo=UTC),
                        },
                    },
                ]
            }

        def get_execution(self, **kwargs):
            assert kwargs == {
                "ExportArn": "arn:aws:bcm-data-exports:us-east-1:123456789012:export/glacier",
                "ExecutionId": "execution-0002",
            }
            return {"Export": {"Name": "glacier-export"}}

    monkeypatch.setattr(
        "riverhog_core.services.glacier_billing._create_billing_s3_client",
        lambda config: _FakeS3Client(),
    )
    monkeypatch.setattr(
        "riverhog_core.services.glacier_billing._create_data_exports_client",
        lambda config: _FakeDataExportsClient(),
    )

    view = resolve_glacier_billing(
        _config(
            tmp_path,
            glacier_endpoint_url="https://s3.us-west-2.amazonaws.com",
            glacier_backend="aws",
            glacier_billing_export_bucket="billing-bucket",
            glacier_billing_export_prefix="billing",
            glacier_billing_export_arn=(
                "arn:aws:bcm-data-exports:us-east-1:123456789012:export/glacier"
            ),
        ),
        include=True,
    ).exports

    assert view is not None
    assert view.source == "aws_data_exports_s3"
    assert view.scope == "bucket"
    assert view.export_arn == "arn:aws:bcm-data-exports:us-east-1:123456789012:export/glacier"
    assert view.export_name == "glacier-export"
    assert view.execution_id == "execution-0002"
    assert view.manifest_key == manifest_key
    assert view.billing_period == "2026-04-01..2026-05-01"
    assert view.object_key is None
    assert view.exported_at == "2026-04-28T08:00:00Z"
    assert view.files_read == 2
    assert view.rows_scanned == 4
    assert view.breakdowns[0].unblended_cost_usd == 2.0
    assert view.breakdowns[0].usage_quantity == 150.0
    assert view.breakdowns[0].resource_id == "arn:aws:s3:::riverhog"
    assert "latest successful execution" in view.notes[0]


def test_resolve_glacier_billing_aggregates_multi_entry_zip_objects(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest = json.dumps(
        {
            "reportKeys": ["billing/20260401-20260501/report.zip"],
        }
    ).encode("utf-8")
    report_one = "\n".join(
        [
            "line_item_product_code,line_item_usage_type,line_item_operation,"
            "line_item_resource_id,line_item_unblended_cost,line_item_usage_amount,"
            "line_item_usage_unit",
            "AmazonS3,TimedStorage-GlacierByteHrs,StandardStorage,riverhog,1.25,100,GB-Mo",
        ]
    ).encode("utf-8")
    report_two = "\n".join(
        [
            "line_item_product_code,line_item_usage_type,line_item_operation,"
            "line_item_resource_id,line_item_unblended_cost,line_item_usage_amount,"
            "line_item_usage_unit",
            "AmazonS3,TimedStorage-GlacierByteHrs,StandardStorage,riverhog,0.75,50,GB-Mo",
        ]
    ).encode("utf-8")
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("report-0001.csv", report_one)
        archive.writestr("report-0002.csv", report_two)

    class _Body:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

    class _Paginator:
        def paginate(self, **kwargs):
            return [
                {
                    "Contents": [
                        {
                            "Key": "billing/20260401-20260501/cur-manifest.json",
                            "LastModified": datetime(2026, 4, 28, tzinfo=UTC),
                        }
                    ]
                }
            ]

    class _FakeS3Client:
        def get_paginator(self, name: str):
            assert name == "list_objects_v2"
            return _Paginator()

        def get_object(self, **kwargs):
            if kwargs["Key"] == "billing/20260401-20260501/cur-manifest.json":
                return {"Body": _Body(manifest)}
            assert kwargs["Key"] == "billing/20260401-20260501/report.zip"
            return {"Body": _Body(archive_buffer.getvalue())}

    monkeypatch.setattr(
        "riverhog_core.services.glacier_billing._create_billing_s3_client",
        lambda config: _FakeS3Client(),
    )

    view = resolve_glacier_billing(
        _config(
            tmp_path,
            glacier_endpoint_url="https://s3.us-west-2.amazonaws.com",
            glacier_backend="aws",
            glacier_billing_export_bucket="billing-bucket",
            glacier_billing_export_prefix="billing",
        ),
        include=True,
    ).exports

    assert view is not None
    assert view.source == "aws_cur_s3"
    assert view.object_key == "billing/20260401-20260501/report.zip"
    assert view.files_read == 1
    assert view.rows_scanned == 2
    assert view.breakdowns[0].unblended_cost_usd == 2.0
    assert view.breakdowns[0].usage_quantity == 150.0
    assert view.breakdowns[0].usage_unit == "GB-Mo"


def test_resolve_glacier_billing_reads_invoice_summaries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class _FakeStsClient:
        def get_caller_identity(self):
            return {"Account": "123456789012"}

    class _FakeInvoicingClient:
        def list_invoice_summaries(self, **kwargs):
            assert kwargs["Selector"] == {
                "ResourceType": "ACCOUNT_ID",
                "Value": "123456789012",
            }
            return {
                "InvoiceSummaries": [
                    {
                        "AccountId": "123456789012",
                        "InvoiceId": "INV-001",
                        "InvoiceType": "Invoice",
                        "BillingPeriod": {"Year": 2026, "Month": 4},
                        "IssuedDate": 1714521600,
                        "DueDate": 1715126400,
                        "Entity": {"InvoicingEntity": "Amazon Web Services, Inc."},
                        "BaseCurrencyAmount": {
                            "CurrencyCode": "USD",
                            "TotalAmount": "99.50",
                        },
                        "PaymentCurrencyAmount": {
                            "CurrencyCode": "USD",
                            "TotalAmount": "99.50",
                        },
                    }
                ]
            }

    monkeypatch.setattr(
        "riverhog_core.services.glacier_billing._create_cost_explorer_client",
        lambda config: (_ for _ in ()).throw(RuntimeError("ce unavailable")),
    )
    monkeypatch.setattr(
        "riverhog_core.services.glacier_billing._create_sts_client",
        lambda config: _FakeStsClient(),
    )
    monkeypatch.setattr(
        "riverhog_core.services.glacier_billing._create_invoicing_client",
        lambda config: _FakeInvoicingClient(),
    )
    monkeypatch.setattr(
        "riverhog_core.services.glacier_billing._resolve_billing_exports",
        lambda config: None,
    )

    summary = resolve_glacier_billing(
        _config(
            tmp_path,
            glacier_endpoint_url="https://s3.us-west-2.amazonaws.com",
            glacier_backend="aws",
        ),
        include=True,
    )

    assert summary is not None
    assert summary.invoices is not None
    assert summary.invoices.source == "aws_invoicing"
    assert summary.invoices.account_id == "123456789012"
    assert summary.invoices.invoices[0].invoice_id == "INV-001"
    assert summary.invoices.invoices[0].base_total_amount == 99.5
