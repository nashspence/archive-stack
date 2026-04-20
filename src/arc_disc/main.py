from __future__ import annotations

from typing import Annotated, Any

import typer

from arc_cli.client import ApiClient
from arc_cli.output import emit

app = typer.Typer(help="arc optical recovery CLI")


class PlaceholderOpticalReader:
    def read(self, disc_path: str, *, device: str) -> bytes:
        raise NotImplementedError(f"optical read not implemented for {disc_path} on {device}")


class PlaceholderCrypto:
    def decrypt_entry(self, encrypted: bytes, enc: dict[str, Any]) -> bytes:
        raise NotImplementedError("entry decryption is not implemented")


@app.command("fetch")
def fetch_cmd(
    fetch_id: Annotated[str, typer.Argument(help="Fetch id")],
    device: Annotated[str, typer.Option("--device", help="Optical device path")] = "/dev/sr0",
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    client = ApiClient()
    manifest = client.get_fetch_manifest(fetch_id)
    reader = PlaceholderOpticalReader()
    crypto = PlaceholderCrypto()

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
