from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from stove0_observer_client import ContentObserverClient, ObserverProtocolError
from stove0_review_sampler_client import ReviewSamplerClient, SamplerProtocolError
from stove0_target_client import TargetClient, TargetProtocolError


def _error_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        400,
        json={
            "error": {
                "code": "bad_request",
                "message": "request rejected",
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

    assert caught.value.message == "request rejected"
    assert caught.value.failure_kind == "remote_rejection"
    assert caught.value.code == "bad_request"
    assert caught.value.observed_status == 400
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

    assert caught.value.message == "request rejected"
    assert caught.value.failure_kind == "remote_rejection"
    assert caught.value.code == "bad_request"
    assert caught.value.observed_status == 400
    assert caught.value.details == {"expected": "a" * 64}


@pytest.mark.parametrize(
    "error_type",
    (TargetProtocolError, ObserverProtocolError, SamplerProtocolError),
)
def test_role_local_errors_do_not_claim_remote_identity(
    error_type: type[TargetProtocolError | ObserverProtocolError | SamplerProtocolError],
) -> None:
    error = error_type("invalid peer response", failure_kind="invalid_response")
    assert error.failure_kind == "invalid_response"
    assert error.code is None
    assert error.observed_status is None


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
def test_role_clients_classify_undeclared_remote_errors_as_invalid_responses(
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
        return httpx.Response(
            409,
            json={"error": {"code": "contract_mismatch", "message": "changed"}},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(httpx.Client, "request", request)

    with pytest.raises(error_type) as caught:
        invoke(client_factory())

    assert caught.value.failure_kind == "invalid_response"
    assert caught.value.code is None
    assert caught.value.observed_status is None


def test_sampler_client_classifies_undeclared_remote_errors_as_invalid_response() -> None:
    def response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"error": {"code": "contract_mismatch", "message": "changed"}},
            request=request,
        )

    with ReviewSamplerClient(
        "https://sampler.invalid",
        "secret",
        transport=httpx.MockTransport(response),
    ) as client:
        with pytest.raises(SamplerProtocolError) as caught:
            client.descriptor()

    assert caught.value.failure_kind == "invalid_response"
    assert caught.value.code is None
    assert caught.value.observed_status is None
