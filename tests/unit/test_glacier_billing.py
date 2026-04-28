from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from arc_core.runtime_config import RuntimeConfig
from arc_core.services.glacier_billing import resolve_glacier_billing


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
    assert summary.source == "unavailable"
    assert summary.scope == "unavailable"
    assert not summary.actuals
    assert not summary.forecast


def test_resolve_glacier_billing_uses_cost_explorer_service_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    recorded: dict[str, object] = {}

    class _FakeCostExplorerClient:
        def get_cost_and_usage(self, **kwargs):
            recorded["actual"] = kwargs
            return {
                "ResultsByTime": [
                    {
                        "TimePeriod": {"Start": "2026-03-01", "End": "2026-04-01"},
                        "Estimated": False,
                        "Total": {
                            "UnblendedCost": {"Amount": "12.34", "Unit": "USD"},
                            "UsageQuantity": {"Amount": "56.78", "Unit": "N/A"},
                        },
                    }
                ]
            }

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

    monkeypatch.setattr(
        "arc_core.services.glacier_billing._create_cost_explorer_client",
        lambda config: _FakeCostExplorerClient(),
    )
    summary = resolve_glacier_billing(
        _config(
            tmp_path,
            glacier_endpoint_url="https://s3.us-west-2.amazonaws.com",
        ),
        include=True,
    )

    assert summary is not None
    assert summary.source == "aws_cost_explorer"
    assert summary.scope == "service"
    assert summary.filter_label == "Amazon Simple Storage Service in us-west-2"
    assert summary.actuals[0].unblended_cost_usd == 12.34
    assert summary.forecast[0].mean_cost_usd == 14.5
    actual_filter = recorded["actual"]["Filter"]
    assert actual_filter == {
        "And": [
            {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Simple Storage Service"]}},
            {"Dimensions": {"Key": "REGION", "Values": ["us-west-2"]}},
        ]
    }
    forecast_filter = recorded["forecast"]["Filter"]
    assert forecast_filter == actual_filter


def test_resolve_glacier_billing_uses_tag_scope_when_configured(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class _FakeCostExplorerClient:
        def get_cost_and_usage(self, **kwargs):
            return {"ResultsByTime": []}

        def get_cost_forecast(self, **kwargs):
            return {"ForecastResultsByTime": []}

    monkeypatch.setattr(
        "arc_core.services.glacier_billing._create_cost_explorer_client",
        lambda config: _FakeCostExplorerClient(),
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
    assert summary.scope == "tag"
    assert summary.filter_label == "backup_set=optical_archive"
    assert not summary.notes
