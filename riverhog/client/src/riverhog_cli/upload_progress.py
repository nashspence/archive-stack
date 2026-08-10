from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from riverhog_cli_support.output import plain_output_requested

from riverhog_cli.output import ATTENTION_STYLE, ENTITY_ID_STYLE, FIELD_STYLE

RichBarColumn: Any
RichConsole: Any
RichGroup: Any
RichLive: Any
RichProgress: Any
RichTable: Any
RichText: Any
RichTextColumn: Any

try:
    from rich.console import Console as RichConsole
    from rich.console import Group as RichGroup
    from rich.live import Live as RichLive
    from rich.progress import BarColumn as RichBarColumn
    from rich.progress import Progress as RichProgress
    from rich.progress import TextColumn as RichTextColumn
    from rich.table import Table as RichTable
    from rich.text import Text as RichText
except ModuleNotFoundError:  # pragma: no cover - exercised only in stripped environments
    RichBarColumn = None
    RichConsole = None
    RichGroup = None
    RichLive = None
    RichProgress = None
    RichTable = None
    RichText = None
    RichTextColumn = None


def _rich_progress_available() -> bool:
    return (
        RichBarColumn is not None
        and RichConsole is not None
        and RichGroup is not None
        and RichLive is not None
        and RichProgress is not None
        and RichTable is not None
        and RichText is not None
        and RichTextColumn is not None
        and not plain_output_requested("RIVERHOG_CLI_PLAIN")
    )


def _format_bytes(value: int | float | None) -> str:
    amount = float(value or 0)
    if amount < 1000:
        return f"{int(amount)} B"
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        amount /= 1000.0
        if amount < 1000.0 or unit == "PB":
            return f"{amount:.1f} {unit}"
    raise AssertionError("unreachable")


def _format_rate(value: int | float | None) -> str:
    return f"{_format_bytes(value)}/s"


def _percent(done: int, total: int) -> float:
    if total <= 0:
        return 100.0 if done > 0 else 0.0
    return min(max(done / total * 100.0, 0.0), 100.0)


def _collection_label(collection_id: int | None) -> str:
    return str(collection_id) if collection_id is not None else "allocating"


def _attention_needed(value: str) -> bool:
    normalized = value.casefold().replace("-", "_")
    return any(
        token in normalized
        for token in ("failed", "failure", "partial", "pending", "retry", "interrupted")
    )


@dataclass(frozen=True, slots=True)
class CollectionUploadProgressState:
    collection_id: int | None
    phase: str
    files_uploaded: int
    files_total: int
    files_hashed: int
    files_registered: int
    uploaded_bytes: int
    bytes_total: int
    rate_bytes_per_second: int
    file_concurrency: int
    chunk_bytes: int
    discovery_complete: bool = True
    notice: str = ""


class UploadProgressRenderer:
    def start(self, state: CollectionUploadProgressState) -> None:
        self.update(state, force=True)

    def update(self, state: CollectionUploadProgressState, *, force: bool = False) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        return


class PlainUploadProgressRenderer(UploadProgressRenderer):
    def __init__(self) -> None:
        self.last_line = ""

    def update(self, state: CollectionUploadProgressState, *, force: bool = False) -> None:
        line = format_upload_progress_line(state)
        if force or line != self.last_line:
            print(line, file=sys.stderr)
            self.last_line = line


class RichUploadProgressRenderer(UploadProgressRenderer):
    def __init__(self) -> None:
        color_system: Literal["auto"] | None = "auto" if sys.stderr.isatty() else None
        self.console = RichConsole(stderr=True, color_system=color_system, highlight=False)
        self.live = RichLive(
            self._render(
                CollectionUploadProgressState(
                    collection_id=None,
                    phase="preparing",
                    files_uploaded=0,
                    files_total=0,
                    files_hashed=0,
                    files_registered=0,
                    uploaded_bytes=0,
                    bytes_total=0,
                    rate_bytes_per_second=0,
                    file_concurrency=1,
                    chunk_bytes=0,
                )
            ),
            console=self.console,
            transient=True,
            refresh_per_second=4,
            auto_refresh=False,
        )
        self.started = False

    def start(self, state: CollectionUploadProgressState) -> None:
        if not self.started:
            self.live.start(refresh=True)
            self.started = True
        self.update(state, force=True)

    def update(self, state: CollectionUploadProgressState, *, force: bool = False) -> None:
        if not self.started:
            self.start(state)
            return
        self.live.update(self._render(state), refresh=True)

    def stop(self) -> None:
        if self.started:
            self.live.stop()
            self.started = False

    def _render(self, state: CollectionUploadProgressState) -> Any:
        title = RichText("collection upload ", style="bold")
        title.append(_collection_label(state.collection_id), style=ENTITY_ID_STYLE)

        table = RichTable.grid(padding=(0, 2))
        table.add_column(justify="right", style=FIELD_STYLE, no_wrap=True, width=14)
        table.add_column(ratio=1)

        phase_text = RichText(state.phase)
        if _attention_needed(state.phase):
            phase_text.stylize(ATTENTION_STYLE)
        table.add_row("State", phase_text)
        if state.discovery_complete:
            table.add_row("Files", self._bar(state.files_uploaded, state.files_total))
            table.add_row("", f"{state.files_uploaded}/{state.files_total} files")
            table.add_row("Bytes", self._bar(state.uploaded_bytes, state.bytes_total))
            table.add_row(
                "",
                (
                    f"{_format_bytes(state.uploaded_bytes)} / {_format_bytes(state.bytes_total)} "
                    f"({_percent(state.uploaded_bytes, state.bytes_total):.1f}%)"
                ),
            )
        else:
            table.add_row(
                "Files",
                f"{state.files_uploaded}/{state.files_total} discovered; final total open",
            )
            table.add_row(
                "Bytes",
                f"{_format_bytes(state.uploaded_bytes)} / "
                f"{_format_bytes(state.bytes_total)} discovered; final total open",
            )
        table.add_row(
            "Pipeline",
            (
                f"{state.files_total} discovered, {state.files_hashed} hashed, "
                f"{state.files_registered} registered, {state.files_uploaded} uploaded"
            ),
        )
        if state.rate_bytes_per_second:
            table.add_row("Rate", _format_rate(state.rate_bytes_per_second))
        table.add_row(
            "Transport",
            f"{state.file_concurrency} worker(s), {_format_bytes(state.chunk_bytes)} chunks",
        )
        if state.notice:
            notice = RichText(state.notice)
            if _attention_needed(state.notice):
                notice.stylize(ATTENTION_STYLE)
            table.add_row("Notice", notice)

        return RichGroup(title, table)

    def _bar(self, done: int, total: int) -> Any:
        pct = _percent(done, total)
        bar = RichProgress(
            RichTextColumn(""),
            RichBarColumn(bar_width=None),
            RichTextColumn(f"{pct:.1f}%"),
            expand=True,
        )
        bar.add_task("", total=100, completed=pct)
        return bar


def format_upload_progress_line(state: CollectionUploadProgressState) -> str:
    if state.discovery_complete:
        files = f"{state.files_uploaded}/{state.files_total} files"
        bytes_value = (
            f"{_format_bytes(state.uploaded_bytes)} / {_format_bytes(state.bytes_total)} "
            f"({_percent(state.uploaded_bytes, state.bytes_total):.1f}%)"
        )
    else:
        files = f"{state.files_uploaded}/{state.files_total} discovered files; final total open"
        bytes_value = (
            f"{_format_bytes(state.uploaded_bytes)} / {_format_bytes(state.bytes_total)} "
            "discovered; final total open"
        )
    pieces = [
        f"collection upload {_collection_label(state.collection_id)}",
        state.phase,
        files,
        bytes_value,
        (
            f"pipeline={state.files_total} discovered/{state.files_hashed} hashed/"
            f"{state.files_registered} registered/{state.files_uploaded} uploaded"
        ),
    ]
    if state.rate_bytes_per_second:
        pieces.append(_format_rate(state.rate_bytes_per_second))
    pieces.append(f"{state.file_concurrency} worker(s)")
    pieces.append(f"{_format_bytes(state.chunk_bytes)} chunks")
    if state.notice:
        pieces.append(state.notice)
    return ", ".join(pieces)


class CollectionUploadProgress:
    def __init__(
        self,
        *,
        collection_id: int,
        files_total: int,
        bytes_total: int,
        files_uploaded: int = 0,
        files_hashed: int | None = None,
        files_registered: int | None = None,
        uploaded_bytes: int = 0,
        file_concurrency: int = 1,
        chunk_bytes: int = 0,
        discovery_complete: bool = True,
        renderer: UploadProgressRenderer | None = None,
        interval_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.collection_id = collection_id
        self.files_total = files_total
        self.bytes_total = bytes_total
        self.files_uploaded = files_uploaded
        self.files_hashed = files_total if files_hashed is None else files_hashed
        self.files_registered = files_total if files_registered is None else files_registered
        self.uploaded_bytes = uploaded_bytes
        self.file_concurrency = file_concurrency
        self.chunk_bytes = chunk_bytes
        self.discovery_complete = discovery_complete
        self.phase = "uploading" if discovery_complete else "discovering/uploading"
        self.notice_text = ""
        self.renderer = renderer or PlainUploadProgressRenderer()
        self.interval_seconds = interval_seconds
        self.clock = clock
        self.started_at = clock()
        self.last_rendered_at = self.started_at
        self.accepted_bytes_this_run = 0
        self.lock = threading.Lock()

    def __enter__(self) -> CollectionUploadProgress:
        self.renderer.start(self._state())
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.renderer.stop()

    def set_totals(self, *, files_total: int, bytes_total: int) -> None:
        with self.lock:
            self.files_total = max(files_total, self.files_uploaded)
            self.bytes_total = max(bytes_total, self.uploaded_bytes)
            self._render_locked(force=False)

    def hashed_file(self) -> None:
        with self.lock:
            self.files_hashed = min(self.files_hashed + 1, max(self.files_total, 0))
            self._render_locked(force=False)

    def registered_file(self) -> None:
        with self.lock:
            self.files_registered = min(self.files_registered + 1, max(self.files_total, 0))
            self._render_locked(force=False)

    def finish_discovery(self) -> None:
        with self.lock:
            self.discovery_complete = True
            self.phase = "uploading"
            self._render_locked(force=True)

    def uploaded(self, delta: int) -> None:
        if delta == 0:
            return
        with self.lock:
            self.accepted_bytes_this_run = max(0, self.accepted_bytes_this_run + delta)
            self.uploaded_bytes = min(
                max(0, self.uploaded_bytes + delta),
                max(self.bytes_total, 0),
            )
            self._render_locked(force=False)

    def resumed(self, delta: int) -> None:
        if delta <= 0:
            return
        with self.lock:
            self.uploaded_bytes = min(self.uploaded_bytes + delta, max(self.bytes_total, 0))
            self._render_locked(force=False)

    def complete_file(self) -> None:
        with self.lock:
            self.files_uploaded = min(self.files_uploaded + 1, max(self.files_total, 0))
            self._render_locked(force=False)

    def notice(self, message: str, *, phase: str | None = None, force: bool = True) -> None:
        with self.lock:
            if phase is not None:
                self.phase = phase
            self.notice_text = message
            self._render_locked(force=force)

    def _state(self) -> CollectionUploadProgressState:
        elapsed = max(self.clock() - self.started_at, 0.001)
        return CollectionUploadProgressState(
            collection_id=self.collection_id,
            phase=self.phase,
            files_uploaded=self.files_uploaded,
            files_total=self.files_total,
            files_hashed=self.files_hashed,
            files_registered=self.files_registered,
            uploaded_bytes=self.uploaded_bytes,
            bytes_total=self.bytes_total,
            rate_bytes_per_second=int(self.accepted_bytes_this_run / elapsed),
            file_concurrency=self.file_concurrency,
            chunk_bytes=self.chunk_bytes,
            discovery_complete=self.discovery_complete,
            notice=self.notice_text,
        )

    def _render_locked(self, *, force: bool) -> None:
        now = self.clock()
        if not force and now - self.last_rendered_at < self.interval_seconds:
            return
        self.renderer.update(self._state(), force=force)
        self.last_rendered_at = now


def make_collection_upload_progress(
    *,
    collection_id: int,
    files_total: int,
    bytes_total: int,
    files_uploaded: int = 0,
    files_hashed: int | None = None,
    files_registered: int | None = None,
    uploaded_bytes: int = 0,
    file_concurrency: int = 1,
    chunk_bytes: int = 0,
    discovery_complete: bool = True,
    json_mode: bool = False,
    interval_seconds: float = 5.0,
) -> CollectionUploadProgress:
    renderer: UploadProgressRenderer
    if json_mode or not _rich_progress_available() or not sys.stderr.isatty():
        renderer = PlainUploadProgressRenderer()
    else:
        try:
            renderer = RichUploadProgressRenderer()
        except Exception:
            renderer = PlainUploadProgressRenderer()
    return CollectionUploadProgress(
        collection_id=collection_id,
        files_total=files_total,
        bytes_total=bytes_total,
        files_uploaded=files_uploaded,
        files_hashed=files_hashed,
        files_registered=files_registered,
        uploaded_bytes=uploaded_bytes,
        file_concurrency=file_concurrency,
        chunk_bytes=chunk_bytes,
        discovery_complete=discovery_complete,
        renderer=renderer,
        interval_seconds=interval_seconds,
    )
