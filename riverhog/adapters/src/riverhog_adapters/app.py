"""Service API and operator entrypoint for supported Riverhog adapters."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import logging
import secrets
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import Body, Depends, FastAPI, Header, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from http_api_contracts import (
    ErrorResponse,
    HealthResponse,
    apply_openapi_error_contract,
    error_code_for_status,
    error_payload,
)
from riverhog_adapter_api_client import RiverhogAdapterClient
from riverhog_api_client import ApiClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from riverhog_adapters.config import AdapterConfig, load_config
from riverhog_adapters.landing import FinalizedReceiptAdapter
from riverhog_adapters.tus import TusAdapterError, TusAuthenticationError, TusPublicationService

BEARER = HTTPBearer(auto_error=False, scheme_name="RiverhogAdaptersBearer")
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AdapterComposition:
    config: AdapterConfig
    api: ApiClient
    adapter: FinalizedReceiptAdapter
    tus: TusPublicationService

    @classmethod
    def build(cls, config: AdapterConfig) -> AdapterComposition:
        api = ApiClient(
            base_url=config.riverhog_base_url,
            token=config.riverhog_token,
            allow_insecure_http=config.allow_insecure_http,
        )
        adapter = FinalizedReceiptAdapter(api, config)
        return cls(
            config=config, api=api, adapter=adapter, tus=TusPublicationService(config, adapter)
        )


class AdapterHttpError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = dict(headers or {})


def create_app(composition: AdapterComposition | None = None) -> FastAPI:
    resolved = composition or AdapterComposition.build(load_config())

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_scheduler(resolved))
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            resolved.api.close()

    app = FastAPI(
        title="Riverhog protocol adapters API",
        version="1.0.0",
        description="Content-opaque FTP, watched-drop, and TUS collection producers.",
        lifespan=lifespan,
    )
    app.state.adapters = resolved

    @app.exception_handler(AdapterHttpError)
    async def adapter_http_error(_request: Request, exc: AdapterHttpError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content=error_payload(code=exc.code, message=exc.message),
            headers=exc.headers,
        )

    @app.exception_handler(TusAuthenticationError)
    async def tus_auth_error(_request: Request, _exc: TusAuthenticationError) -> JSONResponse:
        return JSONResponse(
            status_code=HTTPStatus.UNAUTHORIZED,
            content=error_payload(
                code="unauthorized", message="valid adapter source credentials are required"
            ),
            headers={"WWW-Authenticate": 'Basic realm="Riverhog intake"'},
        )

    @app.exception_handler(TusAdapterError)
    async def tus_adapter_error(_request: Request, exc: TusAdapterError) -> JSONResponse:
        return JSONResponse(
            status_code=HTTPStatus.BAD_REQUEST,
            content=error_payload(code="bad_request", message=str(exc)),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=HTTPStatus.BAD_REQUEST,
            content=error_payload(code="bad_request", message=str(exc)),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(
                code=error_code_for_status(exc.status_code),
                message=str(exc.detail),
            ),
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        LOGGER.exception("unhandled Riverhog adapter API error", exc_info=exc)
        return JSONResponse(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            content=error_payload(code="internal_error", message="internal server error"),
        )

    def management_auth(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(BEARER)],
    ) -> None:
        if (
            credentials is not None
            and credentials.scheme.casefold() == "bearer"
            and secrets.compare_digest(credentials.credentials, resolved.config.api_token)
        ):
            return
        raise AdapterHttpError(
            401,
            "unauthorized",
            "valid adapter bearer credentials are required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.get(
        "/health/live",
        response_model=HealthResponse,
        operation_id="adapter_health_live",
        tags=["health"],
    )
    def health_live() -> HealthResponse:
        return HealthResponse(service="riverhog-adapters", status="ok")

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": ErrorResponse}},
        operation_id="adapter_health_ready",
        tags=["health"],
    )
    def health_ready() -> HealthResponse:
        try:
            resolved.api.list_archive_stores(per_page=1)
        except Exception as exc:
            raise AdapterHttpError(
                503,
                "service_unavailable",
                "Riverhog archive authority is not ready",
            ) from exc
        return HealthResponse(service="riverhog-adapters", status="ok")

    @app.get(
        "/v1/status",
        operation_id="get_adapter_status",
        dependencies=[Depends(management_auth)],
        tags=["service"],
    )
    def status() -> dict[str, object]:
        return resolved.adapter.status()

    @app.post(
        "/v1/run",
        operation_id="run_adapter_pass",
        dependencies=[Depends(management_auth)],
        tags=["operations"],
    )
    def run_pass() -> dict[str, object]:
        return resolved.adapter.run_once()

    @app.post(
        "/v1/sources/{source_id}/flush",
        operation_id="flush_adapter_source",
        dependencies=[Depends(management_auth)],
        tags=["operations"],
    )
    def flush(source_id: str) -> dict[str, object]:
        try:
            return resolved.adapter.flush(source_id)
        except KeyError as exc:
            raise AdapterHttpError(404, "not_found", "adapter source was not found") from exc

    @app.put(
        "/v1/tus-publications/{upload_id}/journals/{journal_id}",
        operation_id="put_tus_provenance_journal",
        openapi_extra={"x-riverhog-interface": "client-only-primitive"},
        tags=["tus"],
    )
    def put_tus_journal(
        upload_id: str,
        journal_id: str,
        content: Annotated[bytes, Body(media_type="application/json-seq")],
        authorization: Annotated[str | None, Header()] = None,
        x_content_sha256: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        return resolved.tus.put_journal(
            upload_id,
            journal_id,
            authorization=authorization,
            content=content,
            expected_sha256=x_content_sha256 or "",
        )

    @app.put(
        "/v1/tus-publications/{upload_id}/binding",
        operation_id="put_tus_provenance_binding",
        openapi_extra={"x-riverhog-interface": "client-only-primitive"},
        tags=["tus"],
    )
    def put_tus_binding(
        upload_id: str,
        binding: Annotated[dict[str, object], Body()],
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        return resolved.tus.put_binding(upload_id, binding, authorization=authorization)

    @app.get(
        "/v1/tus-publications/{upload_id}",
        operation_id="get_tus_publication",
        openapi_extra={"x-riverhog-interface": "client-only-primitive"},
        tags=["tus"],
    )
    def get_tus_publication(
        upload_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        return resolved.tus.receipt(upload_id, authorization=authorization)

    @app.post(
        "/internal/tus/hooks",
        operation_id="handle_tus_hook",
        include_in_schema=False,
    )
    def tus_hook(
        payload: Annotated[dict[str, object], Body()],
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        try:
            return resolved.tus.handle_hook(payload, authorization=authorization)
        except TusAuthenticationError:
            return _hook_rejection(HTTPStatus.UNAUTHORIZED, "invalid adapter source credentials")
        except TusAdapterError:
            return _hook_rejection(HTTPStatus.BAD_REQUEST, "invalid TUS upload")

    @app.get(
        "/internal/tus/auth",
        include_in_schema=False,
    )
    def tus_auth(
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        resolved.tus.authenticate(authorization)
        return Response(status_code=HTTPStatus.NO_CONTENT)

    app.openapi_schema = apply_openapi_error_contract(app.openapi())
    return app


async def _scheduler(composition: AdapterComposition) -> None:
    while True:
        try:
            await asyncio.to_thread(composition.adapter.run_once)
        except Exception:
            LOGGER.exception("Riverhog adapter polling pass failed")
        await asyncio.sleep(composition.config.poll_seconds)


def _hook_rejection(status: HTTPStatus, message: str) -> dict[str, object]:
    return {
        "RejectUpload": True,
        "HTTPResponse": {
            "StatusCode": int(status),
            "Body": message,
            "Header": {"Content-Type": "text/plain"},
        },
    }


def _print(payload: Mapping[str, object], *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    if payload.get("format") == "riverhog-adapters-status/v1":
        print("Riverhog protocol adapters")
        rows = payload.get("sources")
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, Mapping):
                print(
                    f"- {row.get('id')}: {row.get('adapter')}  "
                    f"claims={row.get('claims')}  scratch={row.get('claim_bytes')} bytes"
                )
        return
    print(json.dumps(payload, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="riverhog-adapters")
    parser.add_argument(
        "--version",
        action="version",
        version=importlib.metadata.version("riverhog-adapters"),
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--token")
    parser.add_argument("--allow-insecure-http", action="store_true", default=None)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="run the adapter API and background poller")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8082)
    run = sub.add_parser("run", help="run one landing-source pass")
    run.set_defaults(func=_run_command)
    status = sub.add_parser("status", help="show bounded scratch and source status")
    status.set_defaults(func=_status_command)
    sub.add_parser("check-config", help="validate connected adapter configuration")
    flush = sub.add_parser("flush", help="explicitly close one source batch")
    flush.add_argument("source")
    flush.set_defaults(func=_flush_command)
    return parser


def _operator_client(args: argparse.Namespace) -> RiverhogAdapterClient:
    return RiverhogAdapterClient(
        base_url=args.base_url,
        token=args.token,
        allow_insecure_http=args.allow_insecure_http,
    )


def _run_command(args: argparse.Namespace) -> None:
    with _operator_client(args) as client:
        payload = client.run_adapter_pass()
    _print(payload, json_mode=args.json)


def _status_command(args: argparse.Namespace) -> None:
    with _operator_client(args) as client:
        payload = client.get_adapter_status()
    _print(payload, json_mode=args.json)


def _flush_command(args: argparse.Namespace) -> None:
    with _operator_client(args) as client:
        payload = client.flush_adapter_source(args.source)
    _print(payload, json_mode=args.json)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "serve"
    if command == "serve":
        config = load_config(args.config)
        composition = AdapterComposition.build(config)
        uvicorn.run(create_app(composition), host=args.host, port=args.port)
        return 0
    if command == "check-config":
        config = load_config(args.config)
        config.source(config.sources[0].id)
        payload = {
            "format": "riverhog-adapters-config-check/v1",
            "status": "ok",
            "sources": len(config.sources),
        }
        _print(payload, json_mode=args.json)
        return 0
    callback = getattr(args, "func", None)
    if callback is None:
        raise RuntimeError(f"adapter command has no implementation: {command}")
    callback(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["AdapterComposition", "create_app", "main"]
