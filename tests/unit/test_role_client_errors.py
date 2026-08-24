from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from stove0_observer_client import ContentObserverClient, ObserverProtocolError
from stove0_review_sampler_client import ReviewSamplerClient, SamplerProtocolError
from stove0_target_client import TargetClient, TargetProtocolError


def _error_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        409,
        json={
            "error": {
                "code": "contract_mismatch",
                "message": "contract changed",
                "details": {"expected": "a" * 64},
            }
        },
        request=request,
    )


@pytest.mark.parametrize(
    ("client_factory", "invoke", "error_type"),
    [
        (
            lambda: TargetClient("https://target.invalid"),
            lambda client: client.contract(),
            TargetProtocolError,
        ),
        (
            lambda: ContentObserverClient("https://observer.invalid"),
            lambda client: client.descriptor(),
            ObserverProtocolError,
        ),
    ],
)
def test_role_clients_preserve_structured_error_envelopes(
    monkeypatch: pytest.MonkeyPatch,
    client_factory: Callable[[], object],
    invoke: Callable[[object], object],
    error_type: type[TargetProtocolError | ObserverProtocolError],
) -> None:
    def request(
        _client: httpx.Client,
        method: str,
        url: str,
        **_kwargs: object,
    ) -> httpx.Response:
        return _error_response(httpx.Request(method, url))

    monkeypatch.setattr(httpx.Client, "request", request)

    with pytest.raises(error_type) as caught:
        invoke(client_factory())

    assert caught.value.message == "contract changed"
    assert caught.value.code == "contract_mismatch"
    assert caught.value.observed_status == 409
    assert caught.value.details == {"expected": "a" * 64}


def test_sampler_client_preserves_the_same_role_local_error_fields() -> None:
    transport = httpx.MockTransport(_error_response)

    with ReviewSamplerClient(
        "https://sampler.invalid",
        "secret",
        transport=transport,
    ) as client:
        with pytest.raises(SamplerProtocolError) as caught:
            client.descriptor()

    assert caught.value.message == "contract changed"
    assert caught.value.code == "contract_mismatch"
    assert caught.value.observed_status == 409
    assert caught.value.details == {"expected": "a" * 64}


@pytest.mark.parametrize(
    "error_type",
    (TargetProtocolError, ObserverProtocolError, SamplerProtocolError),
)
def test_role_local_errors_reject_non_error_http_statuses(
    error_type: type[TargetProtocolError | ObserverProtocolError | SamplerProtocolError],
) -> None:
    with pytest.raises(ValueError, match="4xx or 5xx"):
        error_type("not an error response", observed_status=200)
