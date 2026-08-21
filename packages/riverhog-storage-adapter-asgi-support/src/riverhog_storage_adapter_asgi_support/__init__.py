"""Authenticated ASGI shell for Riverhog storage adapters."""

from riverhog_storage_adapter_asgi_support.app import create_storage_adapter_app

__all__ = ["create_storage_adapter_app"]
