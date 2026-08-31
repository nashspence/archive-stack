"""Rich human and stable JSON commands for every public stove0 operation."""

from __future__ import annotations

import importlib.metadata
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, NoReturn, cast

import typer
from pydantic import BaseModel
from rich.console import Console
from rich.pretty import Pretty
from rich.table import Table
from stove0_api_client import Stove0ApiClient, Stove0ApiError
from stove0_recipe_config import RecipeCatalog

app = typer.Typer(help="Operate stove0 collection workflows.")
work_app = typer.Typer(help="Transformation work.")
recipe_app = typer.Typer(help="Configured recipes.")
evaluation_app = typer.Typer(help="Materialized trials and evaluations.")
event_app = typer.Typer(help="Lifecycle events.")
scheduler_app = typer.Typer(help="Scheduler status and execution.")
selection_app = typer.Typer(help="Exact content-addressed artifact selections.")
app.add_typer(work_app, name="work")
app.add_typer(recipe_app, name="recipe")
app.add_typer(evaluation_app, name="evaluation")
app.add_typer(event_app, name="event")
app.add_typer(scheduler_app, name="scheduler")
app.add_typer(selection_app, name="selection")
console = Console()


@dataclass(frozen=True, slots=True)
class Context:
    client: Stove0ApiClient
    json_output: bool


@app.callback()
def configure(
    context: typer.Context,
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            callback=lambda value: _version_callback(value),
            is_eager=True,
            help="Show the installed version and exit.",
        ),
    ] = None,
    base_url: str | None = typer.Option(None, "--base-url"),
    token: str | None = typer.Option(None, "--token"),
    json_output: bool = typer.Option(False, "--json", help="Emit stable JSON."),
    allow_insecure_http: bool | None = typer.Option(
        None,
        "--allow-insecure-http/--no-allow-insecure-http",
        help="Explicitly allow remote cleartext HTTP.",
    ),
) -> None:
    del version
    context.obj = Context(
        client=Stove0ApiClient(
            base_url=base_url,
            token=token,
            allow_insecure_http=allow_insecure_http,
        ),
        json_output=json_output,
    )


def _version_callback(value: bool | None) -> None:
    if not value:
        return
    typer.echo(importlib.metadata.version("stove0-client"))
    raise typer.Exit()


@app.command("health")
def health(context: typer.Context, ready: bool = typer.Option(False, "--ready")) -> None:
    state = _context(context)
    _call(state, state.client.health_ready if ready else state.client.health_live)


@recipe_app.command("list")
def list_recipes(context: typer.Context) -> None:
    state = _context(context)
    _call(state, state.client.list_recipes, table=("recipes", ("id", "revision", "sha256")))


@recipe_app.command("show")
def show_recipe(
    context: typer.Context,
    recipe_id: str,
    revision: int | None = typer.Option(None),
) -> None:
    state = _context(context)
    _call(state, lambda: state.client.get_recipe(recipe_id, revision=revision))


@recipe_app.command("validate")
def validate_recipe_catalog(
    context: typer.Context,
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
) -> None:
    """Validate a deployment-owned catalog without contacting Stove0."""

    state = _context(context)
    _call(
        state,
        lambda: RecipeCatalog.load(path).validation_document(),
        table=("recipes", ("id", "revision", "sha256")),
    )


@work_app.command("list")
def list_work(
    context: typer.Context,
    page: int = typer.Option(1, min=1),
    per_page: int = typer.Option(25, min=1, max=100),
    phase: str | None = typer.Option(None),
    query: str | None = typer.Option(None, "--query", "-q"),
    sort: str = typer.Option("updated_at"),
    order: str = typer.Option("desc"),
) -> None:
    state = _context(context)
    _call(
        state,
        lambda: state.client.list_work(
            page=page,
            per_page=per_page,
            phase=cast(Any, phase),
            query=query,
            sort=cast(Any, sort),
            order=cast(Any, order),
        ),
        table=(
            "work",
            ("work_id", "phase", "result_kind", "target_state", "result_identity", "revision"),
        ),
    )


@work_app.command("create")
def create_work(
    context: typer.Context,
    recipe_id: str,
    collection_ids: Annotated[list[int], typer.Argument()],
    preview_sha256: str = typer.Option(..., "--preview-sha256"),
    revision: int | None = typer.Option(None),
    intent: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
) -> None:
    state = _context(context)
    _call(
        state,
        lambda: state.client.create_work(
            recipe_id,
            sorted(collection_ids),
            preview_sha256=preview_sha256,
            recipe_revision=revision,
            effective_intent=_document(intent),
        ),
    )


@work_app.command("show")
def show_work(context: typer.Context, work_id: str) -> None:
    state = _context(context)
    _call(state, lambda: state.client.get_work(work_id))


@work_app.command("coordination")
def inspect_work_coordination(context: typer.Context, work_id: str) -> None:
    state = _context(context)
    _call(state, lambda: state.client.inspect_work_coordination(work_id))


@selection_app.command("show")
def get_artifact_selection(
    context: typer.Context,
    selection_sha256: str,
    continuation: str | None = typer.Option(None),
) -> None:
    state = _context(context)
    _call(
        state,
        lambda: state.client.get_artifact_selection(
            selection_sha256,
            continuation=continuation,
        ),
        table=("artifacts", ("id", "role", "path", "bytes")),
    )


@work_app.command("step")
def step_work(context: typer.Context, work_id: str) -> None:
    state = _context(context)
    _call(state, lambda: state.client.step_work(work_id))


@work_app.command("retry")
def retry_work(context: typer.Context, work_id: str) -> None:
    state = _context(context)
    _call(state, lambda: state.client.retry_work(work_id))


@work_app.command("cancel")
def cancel_work(
    context: typer.Context,
    work_id: str,
) -> None:
    state = _context(context)
    _call(state, lambda: state.client.cancel_work(work_id))


@app.command("preview")
def preview(
    context: typer.Context,
    recipe_id: str,
    collection_ids: Annotated[list[int], typer.Argument()],
    revision: int | None = typer.Option(None),
    intent: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
) -> None:
    state = _context(context)
    _call(
        state,
        lambda: state.client.preview_workflow(
            recipe_id,
            sorted(collection_ids),
            recipe_revision=revision,
            effective_intent=_document(intent),
        ),
    )


@evaluation_app.command("list")
def list_evaluations(
    context: typer.Context,
    page: int = typer.Option(1, min=1),
    per_page: int = typer.Option(25, min=1, max=100),
    phase: str | None = typer.Option(None),
    query: str | None = typer.Option(None, "--query", "-q"),
    sort: str = typer.Option("updated_at"),
    order: str = typer.Option("desc"),
) -> None:
    state = _context(context)
    _call(
        state,
        lambda: state.client.list_evaluations(
            page=page,
            per_page=per_page,
            phase=cast(Any, phase),
            query=query,
            sort=cast(Any, sort),
            order=cast(Any, order),
        ),
        table=("evaluations", ("evaluation_id", "phase", "revision")),
    )


@evaluation_app.command("create")
def create_evaluation(context: typer.Context, definition: Path) -> None:
    state = _context(context)
    _call(state, lambda: state.client.create_evaluation(_document(definition)))


@evaluation_app.command("show")
def show_evaluation(context: typer.Context, evaluation_id: str) -> None:
    state = _context(context)
    _call(state, lambda: state.client.get_evaluation(evaluation_id))


@evaluation_app.command("step")
def step_evaluation(context: typer.Context, evaluation_id: str) -> None:
    state = _context(context)
    _call(state, lambda: state.client.step_evaluation(evaluation_id))


@evaluation_app.command("cancel")
def cancel_evaluation(
    context: typer.Context,
    evaluation_id: str,
) -> None:
    state = _context(context)
    _call(state, lambda: state.client.cancel_evaluation(evaluation_id))


@evaluation_app.command("retry")
def retry_evaluation(
    context: typer.Context,
    evaluation_id: str,
    variant_id: str,
) -> None:
    state = _context(context)
    _call(state, lambda: state.client.retry_evaluation_variant(evaluation_id, variant_id))


@evaluation_app.command("review")
def review_evaluation(
    context: typer.Context,
    evaluation_id: str,
    variant_id: str,
    rating: int | None = typer.Option(None, min=1, max=5),
    note: str | None = typer.Option(None),
) -> None:
    state = _context(context)
    _call(
        state,
        lambda: state.client.review_evaluation_variant(
            evaluation_id,
            variant_id,
            rating=rating,
            note=note,
        ),
    )


@event_app.command("list")
def list_events(
    context: typer.Context,
    after: str | None = typer.Option(None),
    limit: int = typer.Option(100, min=1, max=100),
) -> None:
    state = _context(context)
    _call(
        state,
        lambda: state.client.list_events(after=after, limit=limit).model_dump(mode="json"),
        table=("events", ("id", "type", "subject", "time")),
    )


@scheduler_app.command("status")
def scheduler_status(context: typer.Context) -> None:
    state = _context(context)
    _call(state, state.client.scheduler_status)


@scheduler_app.command("run")
def scheduler_run(
    context: typer.Context,
    role: str = typer.Option("combined"),
    event_limit: int = typer.Option(100, min=1, max=100),
    work_limit: int = typer.Option(25, min=1, max=100),
) -> None:
    state = _context(context)
    _call(
        state,
        lambda: state.client.run_scheduler(
            role=cast(Any, role),
            event_limit=event_limit,
            work_limit=work_limit,
        ),
    )


def _context(context: typer.Context) -> Context:
    if not isinstance(context.obj, Context):
        raise RuntimeError("stove0 CLI context was not initialized")
    return context.obj


def _document(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise typer.BadParameter("JSON document must contain an object")
    return value


def _call(
    state: Context,
    operation: Any,
    *,
    table: tuple[str, tuple[str, ...]] | None = None,
) -> None:
    try:
        payload = operation()
    except (Stove0ApiError, ValueError) as exc:
        _fail(str(exc))
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
    _render(payload, json_output=state.json_output, table=table)


def _render(
    payload: dict[str, Any],
    *,
    json_output: bool,
    table: tuple[str, tuple[str, ...]] | None,
) -> None:
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return
    if table is not None and isinstance(payload.get(table[0]), list):
        if table[0] == "recipes" and payload.get("catalog_sha256") is not None:
            console.print(f"Catalog: {payload['catalog_sha256']}")
        rendered = Table(show_header=True)
        for column in table[1]:
            rendered.add_column(column.replace("_", " ").title())
        for item in payload[table[0]]:
            if not isinstance(item, dict):
                continue
            rendered.add_row(*(_table_value(item, column) for column in table[1]))
        console.print(rendered)
        return
    console.print(Pretty(payload, expand_all=False))


def _table_value(item: dict[str, Any], column: str) -> str:
    if column in item:
        return str(item[column])
    definition = item.get("definition")
    if isinstance(definition, dict) and column in {"id", "revision"}:
        return str(definition.get(column, ""))
    status = item.get("target_status")
    workflow = item.get("workflow_plan")
    branch_set = item.get("branch_set_plan")
    coordination = item.get("coordination_settlement")
    if column == "result_kind":
        if isinstance(branch_set, dict):
            return "coordination"
        if isinstance(workflow, dict) and workflow.get("result_kind") is not None:
            return str(workflow["result_kind"])
        if isinstance(status, dict):
            return (
                "external-effect"
                if status.get("protocol") == "stove0-effect-target/v1"
                else "collection"
            )
    if column == "target_state" and isinstance(status, dict):
        return str(status.get("state", ""))
    if column == "result_identity":
        if isinstance(coordination, dict):
            return str(coordination.get("settlement_sha256", ""))
        if isinstance(status, dict):
            receipt = status.get("effect_receipt")
            if isinstance(receipt, dict):
                return str(receipt.get("receipt_sha256", ""))
            output = status.get("output_collection")
            if isinstance(output, dict):
                return str(output.get("content_identity", ""))
    return ""


def _fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(1)


def main() -> None:
    app()


__all__ = ["app", "main"]
