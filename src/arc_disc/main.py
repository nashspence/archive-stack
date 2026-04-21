from __future__ import annotations

import importlib
import os
from typing import Annotated, Any

import typer

from arc_cli.client import ApiClient
from arc_cli.output import emit

app = typer.Typer(help="arc optical recovery CLI")


@app.callback()
def arc_disc_app() -> None:
    """Keep the CLI in group mode so `arc-disc fetch ...` stays canonical."""


class PlaceholderOpticalReader:
    def read(self, disc_path: str, *, device: str) -> bytes:
        raise NotImplementedError(f"optical read not implemented for {disc_path} on {device}")


class PlaceholderCrypto:
    def decrypt_entry(self, encrypted: bytes, enc: dict[str, Any]) -> bytes:
        raise NotImplementedError("entry decryption is not implemented")


def _load_factory(spec: str) -> object:
    module_name, sep, attr_name = spec.partition(":")
    if not sep:
        raise RuntimeError(f"invalid factory spec: {spec!r}")
    factory = getattr(importlib.import_module(module_name), attr_name)
    if not callable(factory):
        raise RuntimeError(f"factory must be callable: {spec!r}")
    return factory()


def build_optical_reader() -> object:
    spec = os.getenv("ARC_DISC_READER_FACTORY")
    if spec:
        return _load_factory(spec)
    return PlaceholderOpticalReader()


def build_crypto() -> object:
    spec = os.getenv("ARC_DISC_CRYPTO_FACTORY")
    if spec:
        return _load_factory(spec)
    return PlaceholderCrypto()


@app.command("fetch")
def fetch_cmd(
    fetch_id: Annotated[str, typer.Argument(help="Fetch id")],
    device: Annotated[str, typer.Option("--device", help="Optical device path")] = "/dev/sr0",
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    client = ApiClient()
    manifest = client.get_fetch_manifest(fetch_id)
    reader = build_optical_reader()
    crypto = build_crypto()

    for entry in manifest.get("entries", []):
        copy_info = entry["copies"][0]
        encrypted = reader.read(copy_info["disc_path"], device=device)
        plaintext = crypto.decrypt_entry(encrypted, copy_info["enc"])
        client.upload_fetch_entry(fetch_id, entry["id"], entry["sha256"], plaintext)

    payload = client.complete_fetch(fetch_id)
    emit(payload, json_mode=json_mode)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
