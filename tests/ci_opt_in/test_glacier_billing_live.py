from __future__ import annotations

import os
from dataclasses import replace
from typing import NoReturn

import pytest
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from riverhog_core.runtime_config import RuntimeConfig, load_runtime_config
from riverhog_core.services.glacier_billing import resolve_glacier_billing

_CONFIRM = "live-aws-billing"
pytestmark = [
    pytest.mark.ci_opt_in,
    pytest.mark.requires_aws_billing,
]


def _require_live_billing_confirmation() -> None:
    if os.environ.get("RIVERHOG_GLACIER_BILLING_CI_OPT_IN_CONFIRM") == _CONFIRM:
        return
    pytest.skip(
        "set RIVERHOG_GLACIER_BILLING_CI_OPT_IN_CONFIRM=live-aws-billing to run live "
        "AWS Glacier billing validation"
    )


def _config() -> RuntimeConfig:
    _require_live_billing_confirmation()
    return replace(load_runtime_config(), glacier_billing_mode="aws")


def _skip_unsupported_aws_billing(exc: Exception) -> NoReturn:
    if isinstance(exc, NoCredentialsError):
        pytest.skip("AWS credentials are not available for live billing validation")
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = str(error.get("Code", ""))
        if code in {
            "AccessDenied",
            "AccessDeniedException",
            "UnauthorizedException",
            "UnrecognizedClientException",
            "OptInRequired",
        }:
            pytest.skip(f"AWS billing capability is unavailable: {code}")
    if isinstance(exc, BotoCoreError):
        pytest.skip(f"AWS billing client could not be initialized: {exc}")
    raise exc


def test_live_aws_glacier_billing_resolves_actuals_and_forecast() -> None:
    try:
        summary = resolve_glacier_billing(_config(), include=True)
    except Exception as exc:
        _skip_unsupported_aws_billing(exc)

    assert summary is not None
    assert summary.actuals is not None
    assert summary.actuals.source in {"aws_cost_explorer", "aws_cost_explorer_resource"}
    assert summary.actuals.scope in {"bucket", "tag", "service"}
    assert summary.forecast is not None
    assert summary.forecast.source == "aws_cost_explorer"
    assert summary.forecast.scope in {"tag", "service"}


def test_live_aws_glacier_billing_resolves_configured_export_metadata() -> None:
    config = _config()
    if not config.glacier_billing_export_bucket or not config.glacier_billing_export_prefix:
        pytest.skip(
            "set RIVERHOG_GLACIER_BILLING_EXPORT_BUCKET and "
            "RIVERHOG_GLACIER_BILLING_EXPORT_PREFIX to validate live billing exports"
        )

    try:
        summary = resolve_glacier_billing(config, include=True)
    except Exception as exc:
        _skip_unsupported_aws_billing(exc)

    assert summary is not None
    assert summary.exports is not None
    assert summary.exports.source in {"aws_cur_s3", "aws_data_exports", "unavailable"}
    if summary.exports.source == "unavailable":
        assert summary.exports.notes
    else:
        assert summary.exports.manifest_key
        assert summary.exports.files_read > 0
