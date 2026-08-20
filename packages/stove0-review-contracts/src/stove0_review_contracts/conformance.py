from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from stove0_review_contracts.contracts import (
    MEDIA_SAMPLING_OBSERVER_CONTRACT,
    REVIEW_SAMPLE_ENCODE_OPERATION,
)


def contract_report() -> dict[str, object]:
    return {
        "format": "stove0-review-contract-report/v1",
        "observer_contract": MEDIA_SAMPLING_OBSERVER_CONTRACT.model_dump(mode="json"),
        "operation_contract": REVIEW_SAMPLE_ENCODE_OPERATION.model_dump(mode="json"),
        "source_retirement_permitted": False,
        "status": "conformant",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stove0-review-contracts",
        description="Print the maintained stove0 review contract identities.",
    )
    parser.parse_args(argv)
    print(json.dumps(contract_report(), sort_keys=True, separators=(",", ":")))
    return 0


__all__ = ["contract_report", "main"]
