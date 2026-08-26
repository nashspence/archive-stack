from __future__ import annotations

import uuid

import pytest


def urn() -> str:
    return f"urn:uuid:{uuid.uuid4()}"


@pytest.fixture
def urn_factory():
    return urn
