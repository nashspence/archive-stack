from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy import CheckConstraint, Integer, MetaData, UniqueConstraint, inspect
from sqlalchemy.engine import Connection, Engine


def _type_name(value: Any, bind: Connection | Engine) -> str:
    return " ".join(str(value.compile(dialect=bind.dialect)).upper().split())


def _sql(value: object | None) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).strip().split())
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        wraps = True
        for index, character in enumerate(text):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(text) - 1:
                    wraps = False
                    break
        if not wraps:
            break
        text = text[1:-1].strip()
    return text


def _server_default(column: Any, bind: Connection | Engine) -> str | None:
    if column.identity is not None:
        return None
    default = column.server_default
    if default is None:
        return None
    argument = default.arg
    if isinstance(argument, str):
        return _sql(argument)
    return _sql(argument.compile(dialect=bind.dialect, compile_kwargs={"literal_binds": True}))


def _uses_postgresql_implicit_sequence(column: Any, bind: Connection | Engine) -> bool:
    return (
        bind.dialect.name == "postgresql"
        and column.identity is None
        and column.primary_key
        and isinstance(column.type, Integer)
        and column.table.autoincrement_column is column
    )


def _check_expression(value: object | None) -> str:
    expression = (_sql(value) or "").casefold().replace('"', "")
    expression = re.sub(
        r"::(?:character varying|text|bigint|integer)(?:\[\])?",
        "",
        expression,
    )
    expression = re.sub(
        r"([a-z_][a-z0-9_]*)\s*=\s*any\s*\(\s*array\[(.*?)\]\s*\)",
        r"\1 in (\2)",
        expression,
    )
    expression = re.sub(r"\s*([(),>=])\s*", r"\1", expression)
    return " ".join(expression.split())


def _foreign_key_signature(constraint: Any) -> tuple[object, ...]:
    elements = tuple(constraint.elements)
    return (
        tuple(str(element.parent.name) for element in elements),
        str(elements[0].column.table.name),
        tuple(str(element.column.name) for element in elements),
        str(constraint.ondelete or "").upper(),
        str(constraint.onupdate or "").upper(),
        bool(constraint.deferrable),
        str(constraint.initially or "").upper(),
    )


def _inspected_foreign_key_signature(constraint: Mapping[str, Any]) -> tuple[object, ...]:
    options = constraint.get("options", {})
    return (
        tuple(str(name) for name in constraint["constrained_columns"]),
        str(constraint["referred_table"]),
        tuple(str(name) for name in constraint["referred_columns"]),
        str(options.get("ondelete") or "").upper(),
        str(options.get("onupdate") or "").upper(),
        bool(options.get("deferrable")),
        str(options.get("initially") or "").upper(),
    )


def assert_schema_matches_metadata(
    bind: Connection | Engine,
    metadata: MetaData,
    *,
    version_table: str,
) -> None:
    """Verify the complete current relational shape owned by SQLAlchemy metadata."""

    inspector = inspect(bind)
    expected = {table.name: table for table in metadata.sorted_tables}
    actual_tables = set(inspector.get_table_names()) - {version_table}
    differences: list[str] = []
    differences.extend(
        f"unexpected table {table_name}" for table_name in sorted(actual_tables - set(expected))
    )
    differences.extend(
        f"missing table {table_name}" for table_name in sorted(set(expected) - actual_tables)
    )
    for table_name in sorted(actual_tables & set(expected)):
        table = expected[table_name]
        expected_columns = {column.name: column for column in table.columns}
        inspected_columns = inspector.get_columns(table_name)
        actual_columns = {str(column["name"]): column for column in inspected_columns}
        expected_order = tuple(expected_columns)
        actual_order = tuple(str(column["name"]) for column in inspected_columns)
        if actual_order != expected_order:
            differences.append(
                f"table {table_name} column order is {actual_order}, expected {expected_order}"
            )
        differences.extend(
            f"unexpected column {table_name}.{name}"
            for name in sorted(set(actual_columns) - set(expected_columns))
        )
        differences.extend(
            f"missing column {table_name}.{name}"
            for name in sorted(set(expected_columns) - set(actual_columns))
        )
        for column_name in sorted(set(expected_columns) & set(actual_columns)):
            expected_column = expected_columns[column_name]
            actual_column = actual_columns[column_name]
            expected_type = _type_name(expected_column.type, bind)
            actual_type = _type_name(actual_column["type"], bind)
            if actual_type != expected_type:
                differences.append(
                    f"column {table_name}.{column_name} has type {actual_type}, "
                    f"expected {expected_type}"
                )
            if bool(actual_column["nullable"]) != bool(expected_column.nullable):
                differences.append(
                    f"column {table_name}.{column_name} has nullable="
                    f"{bool(actual_column['nullable'])}, expected {bool(expected_column.nullable)}"
                )
            actual_default = _sql(actual_column.get("default"))
            expected_default: str | None
            if _uses_postgresql_implicit_sequence(expected_column, bind):
                expected_default = "postgresql implicit sequence"
                if actual_default is not None and actual_default.startswith("nextval('"):
                    actual_default = expected_default
            else:
                expected_default = _server_default(expected_column, bind)
            if actual_default != expected_default:
                differences.append(
                    f"column {table_name}.{column_name} has default {actual_default!r}, "
                    f"expected {expected_default!r}"
                )
            if bind.dialect.name != "sqlite":
                actual_identity = actual_column.get("identity")
                expected_identity = expected_column.identity
                if (actual_identity is None) != (expected_identity is None):
                    differences.append(
                        f"column {table_name}.{column_name} identity presence differs"
                    )
                elif actual_identity is not None and expected_identity is not None:
                    if bool(actual_identity.get("always")) != bool(expected_identity.always):
                        differences.append(
                            f"column {table_name}.{column_name} identity mode differs"
                        )
            actual_computed = actual_column.get("computed")
            expected_computed = expected_column.computed
            if (actual_computed is None) != (expected_computed is None):
                differences.append(f"column {table_name}.{column_name} computed value differs")

        expected_primary_key = tuple(str(column.name) for column in table.primary_key.columns)
        actual_primary_key = tuple(
            str(name)
            for name in (inspector.get_pk_constraint(table_name)["constrained_columns"] or ())
        )
        if actual_primary_key != expected_primary_key:
            differences.append(
                f"table {table_name} has primary key {actual_primary_key}, "
                f"expected {expected_primary_key}"
            )

        expected_foreign_keys = {
            _foreign_key_signature(constraint) for constraint in table.foreign_key_constraints
        }
        actual_foreign_keys = {
            _inspected_foreign_key_signature(constraint)
            for constraint in inspector.get_foreign_keys(table_name)
        }
        differences.extend(
            f"unexpected foreign key on {table_name}: {signature}"
            for signature in sorted(actual_foreign_keys - expected_foreign_keys)
        )
        differences.extend(
            f"missing foreign key on {table_name}: {signature}"
            for signature in sorted(expected_foreign_keys - actual_foreign_keys)
        )

        expected_unique_constraints = {
            tuple(str(column.name) for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        actual_unique_constraints = {
            tuple(str(name) for name in constraint["column_names"])
            for constraint in inspector.get_unique_constraints(table_name)
        }
        if actual_unique_constraints != expected_unique_constraints:
            differences.append(
                f"table {table_name} unique constraints are {actual_unique_constraints}, "
                f"expected {expected_unique_constraints}"
            )

        expected_indexes = {
            str(index.name): (
                tuple(str(column.name) for column in index.columns),
                bool(index.unique),
            )
            for index in table.indexes
        }
        actual_indexes = {
            str(index["name"]): (
                tuple(str(name) for name in index["column_names"]),
                bool(index["unique"]),
            )
            for index in inspector.get_indexes(table_name)
            if not index.get("duplicates_constraint")
        }
        if actual_indexes != expected_indexes:
            differences.append(
                f"table {table_name} indexes are {actual_indexes}, expected {expected_indexes}"
            )

        expected_checks = {
            str(constraint.name): _check_expression(
                constraint.sqltext.compile(
                    dialect=bind.dialect,
                    compile_kwargs={"literal_binds": True},
                )
            )
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint) and constraint.name is not None
        }
        actual_checks = {
            str(constraint["name"]): _check_expression(constraint.get("sqltext"))
            for constraint in inspector.get_check_constraints(table_name)
            if constraint["name"] is not None
        }
        if actual_checks != expected_checks:
            differences.append(
                f"table {table_name} checks are {actual_checks}, expected {expected_checks}"
            )
    if differences:
        raise RuntimeError("schema does not match current metadata: " + "; ".join(differences))


__all__ = ["assert_schema_matches_metadata"]
