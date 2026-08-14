from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from jeb_api_client import JebApiClient, JebIngressClient

SOURCE_ID = "compose-source"
SOURCE_CREDENTIAL = "compose-source-test-credential"
RELATIVE_PATH = "notes/compose.txt"
PAYLOAD = b"real Jeb Compose TUS lifecycle\n"


def _binding(payload: Path) -> dict[str, object]:
    return {
        "path": RELATIVE_PATH,
        "bytes": payload.stat().st_size,
        "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
        "status": "omitted",
        "omission_reason": "disposable Compose fixture has no host provenance",
    }


def upload(args: argparse.Namespace) -> None:
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    payload = work_dir / "compose.txt"
    payload.write_bytes(PAYLOAD)
    with JebApiClient(
        args.api_url,
        args.management_token,
        allow_insecure_http=True,
    ) as management:
        created = management.create_source(
            {
                "id": SOURCE_ID,
                "adapters": ["tus"],
                "credential": SOURCE_CREDENTIAL,
                "stable_seconds": 600,
                "target": "munchy",
                "target_config": {"template_id": "compose-smoke"},
                "cleanup": "never",
                "cadence": "manual",
            }
        )
        assert created["source"]["id"] == SOURCE_ID
    with JebIngressClient(
        source=SOURCE_ID,
        password=SOURCE_CREDENTIAL,
        base_url=args.ingress_url,
        allow_insecure_http=True,
    ) as ingress:
        receipt = ingress.upload_file(
            payload,
            relative_path=RELATIVE_PATH,
            binding=_binding(payload),
            journals={},
        )
    assert receipt["status"] == "accepted"
    assert receipt["path"] == RELATIVE_PATH
    assert receipt["bytes"] == len(PAYLOAD)
    assert receipt["payload_sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    assert len(str(receipt["provenance_identity"])) == 64
    (work_dir / "receipt.json").write_text(
        json.dumps(receipt, sort_keys=True),
        encoding="utf-8",
    )


def verify(args: argparse.Namespace) -> None:
    work_dir = Path(args.work_dir)
    receipt = json.loads((work_dir / "receipt.json").read_text(encoding="utf-8"))
    assert isinstance(receipt, dict)
    upload_id = str(receipt["upload_id"])
    with JebIngressClient(
        source=SOURCE_ID,
        password=SOURCE_CREDENTIAL,
        base_url=args.ingress_url,
        allow_insecure_http=True,
    ) as ingress:
        restarted_receipt = ingress.wait_for_publication(upload_id)
    assert restarted_receipt == receipt
    with JebApiClient(
        args.api_url,
        args.management_token,
        allow_insecure_http=True,
    ) as management:
        status: dict[str, Any] = management.get_status(include_backlog=True)
    assert status["ingress_publications"] == {
        "pending": 0,
        "accepted": 1,
        "rejected": 0,
    }
    landing = Path(args.landing_dir)
    assert (landing / SOURCE_ID / RELATIVE_PATH).read_bytes() == PAYLOAD
    staging = landing / ".ingress" / "tus"
    assert not (staging / upload_id).exists()
    assert not (staging / f"{upload_id}.info").exists()
    assert not (staging / ".provenance" / upload_id).exists()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Qualify the real Jeb Compose TUS lifecycle.")
    parser.add_argument("phase", choices=("upload", "verify"))
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--ingress-url", required=True)
    parser.add_argument("--management-token", required=True)
    parser.add_argument("--landing-dir", required=True)
    parser.add_argument("--work-dir", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.phase == "upload":
        upload(args)
    else:
        verify(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
