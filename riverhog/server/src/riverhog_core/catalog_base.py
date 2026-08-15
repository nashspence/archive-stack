from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass

# Load the hard-cut collection workflow tables into the one current v1 metadata.
from riverhog_core import catalog_workflow_models as _catalog_workflow_models  # noqa: E402,F401
