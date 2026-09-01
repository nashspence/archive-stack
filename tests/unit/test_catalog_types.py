from __future__ import annotations

import pytest
from riverhog_core.catalog_types import archive_object_order_type, archive_sequence_type
from sqlalchemy import Column, MetaData, Table, create_engine, insert, select
from sqlalchemy.exc import StatementError


def test_archive_sequence_storage_round_trips_and_orders_full_v1_domain() -> None:
    metadata = MetaData()
    sequences = Table(
        "sequences",
        metadata,
        Column("value", archive_sequence_type(), primary_key=True),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata.create_all(engine)
    values = ((1 << 256) - 1, 1 << 63, (1 << 63) - 1, 0)

    with engine.begin() as connection:
        connection.execute(insert(sequences), [{"value": value} for value in values])
        observed = connection.execute(
            select(sequences.c.value).order_by(sequences.c.value)
        ).scalars()
        stored = connection.exec_driver_sql("SELECT value FROM sequences ORDER BY value").scalars()

        assert list(observed) == sorted(values)
        assert list(stored) == [f"{value:064x}" for value in sorted(values)]


@pytest.mark.parametrize("value", [-1, 1 << 256, True, "1"])
def test_archive_sequence_storage_rejects_out_of_domain_values(value: object) -> None:
    metadata = MetaData()
    sequences = Table(
        "sequences",
        metadata,
        Column("value", archive_sequence_type(), primary_key=True),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata.create_all(engine)

    with engine.begin() as connection, pytest.raises(StatementError):
        connection.execute(insert(sequences).values(value=value))


def test_private_archive_object_order_covers_all_finalized_archive_objects() -> None:
    metadata = MetaData()
    orders = Table(
        "orders",
        metadata,
        Column("value", archive_object_order_type(), primary_key=True),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata.create_all(engine)
    last_valid_archive_order = 1 << 258

    with engine.begin() as connection:
        connection.execute(insert(orders).values(value=last_valid_archive_order))
        assert connection.scalar(select(orders.c.value)) == last_valid_archive_order
        assert connection.exec_driver_sql("SELECT value FROM orders").scalar_one() == (
            "4" + "0" * 64
        )
