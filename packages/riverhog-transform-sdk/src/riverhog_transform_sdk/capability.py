"""Refreshable in-memory Riverhog capability client."""

from __future__ import annotations

import threading
from typing import Any, Self


class _CapabilityClientState:
    def __init__(self, client: Any, *, owned: bool) -> None:
        self.lock = threading.RLock()
        self.current = client
        self.current_owned = owned
        self.retired: list[tuple[Any, bool]] = []
        self.closed = False

    def snapshot(self) -> Any:
        with self.lock:
            if self.closed:
                raise RuntimeError("transform capability client is closed")
            return self.current

    def replace(self, client: Any, *, owned: bool) -> None:
        with self.lock:
            if self.closed:
                _close(client, owned=owned)
                raise RuntimeError("transform capability client is closed")
            self.retired.append((self.current, self.current_owned))
            self.current = client
            self.current_owned = owned

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            values = [*self.retired, (self.current, self.current_owned)]
            self.retired = []
        for client, owned in values:
            _close(client, owned=owned)


class CapabilityApiClient:
    """Stable API facade whose bearer client can be refreshed concurrently.

    Worker facades returned by :meth:`spawn` share the same in-memory state. Each
    HTTP operation resolves the current delegate when it begins, while delegates
    already serving an open stream remain alive until the root facade closes.
    Capability material is never persisted by this class.
    """

    def __init__(
        self,
        client: Any,
        *,
        owns_client: bool = False,
        _state: _CapabilityClientState | None = None,
        _root: bool = True,
    ) -> None:
        self._state = _state or _CapabilityClientState(client, owned=owns_client)
        self._root = _root

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def current(self) -> Any:
        return self._state.snapshot()

    def replace(self, client: Any, *, owns_client: bool = True) -> None:
        if not self._root:
            raise RuntimeError("only the root capability client may replace its delegate")
        self._state.replace(client, owned=owns_client)

    def spawn(self) -> CapabilityApiClient:
        return CapabilityApiClient(
            self.current,
            _state=self._state,
            _root=False,
        )

    def close(self) -> None:
        if self._root:
            self._state.close()

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self.current, name)
        if not callable(attribute):
            return attribute

        def invoke(*args: Any, **kwargs: Any) -> Any:
            return getattr(self.current, name)(*args, **kwargs)

        return invoke


def _close(client: Any, *, owned: bool) -> None:
    if not owned:
        return
    close = getattr(client, "close", None)
    if callable(close):
        close()


__all__ = ["CapabilityApiClient"]
