from __future__ import annotations

import uuid

PACKAGE_NAME = "riverhog-provenance"
PACKAGE_VERSION = "0.1.0"
PROVENANCE_PROFILE = "https://nashspence.github.io/riverhog/v1/provenance"
PROVENANCE_ENTRY_SCHEMA = f"{PROVENANCE_PROFILE}/journal-entry.schema.json"
OBSERVER_PROFILE = f"{PROVENANCE_PROFILE}/observers"
CAPTURE_PLAN_ID = f"{OBSERVER_PROFILE}/archive-file-state-observation"
OBSERVER_NAMESPACE = uuid.UUID("99b19dc2-79dd-4c27-a097-245b3b6b7169")
DEFAULT_OBSERVER_AGENT_ID = (
    f"urn:uuid:{uuid.uuid5(OBSERVER_NAMESPACE, PACKAGE_NAME + ':' + PACKAGE_VERSION)}"
)

COVERAGE_CATEGORIES = (
    "content_fixity",
    "locator",
    "basic_filesystem",
    "timestamps",
    "ownership",
    "permissions",
    "native_identifiers",
    "extended_attributes",
    "access_control",
    "alternate_streams",
    "resource_forks",
    "file_flags",
    "security_metadata",
    "storage_layout",
    "special_file_features",
    "native_metadata_other",
)
