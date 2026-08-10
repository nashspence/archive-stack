from __future__ import annotations


class ProvenanceObserverError(Exception):
    """Base class for observer failures."""


class UnsupportedPlatformError(ProvenanceObserverError):
    """Raised when an observer is used on an unsupported operating system."""


class UnsupportedFileTypeError(ProvenanceObserverError):
    """Raised when the target is not a regular file."""


class SymlinkRefusedError(ProvenanceObserverError):
    """Raised when the target's final path component is a symbolic link."""


class UnstableFileError(ProvenanceObserverError):
    """Raised when a strict capture cannot establish a stable file state."""


class NativeObservationError(ProvenanceObserverError):
    """Raised when a required native observation fails."""


class SchemaValidationUnavailable(ProvenanceObserverError):
    """Raised when schema validation was requested without jsonschema installed."""
