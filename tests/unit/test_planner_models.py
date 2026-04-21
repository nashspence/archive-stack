from __future__ import annotations

from pathlib import Path

from arc_core.planner.models import PlannerCollection, PlannerFile, PlannerPiece


def test_planner_file_piece_count_reflects_the_number_of_pieces() -> None:
    planner_file = PlannerFile(
        file_id=1,
        relpath="/docs/a.txt",
        source=Path("/tmp/a.txt"),
        plaintext_bytes=12,
        mode=None,
        mtime=None,
        uid=None,
        gid=None,
        sha256="a" * 64,
        pieces=[
            PlannerPiece(
                collection="docs",
                file_id=1,
                relpath="/docs/a.txt",
                store_relpath="store/a.001",
                payload_bytes=6,
                piece_index=0,
                piece_count=2,
                estimated_on_disc_bytes=8,
            ),
            PlannerPiece(
                collection="docs",
                file_id=1,
                relpath="/docs/a.txt",
                store_relpath="store/a.002",
                payload_bytes=6,
                piece_index=1,
                piece_count=2,
                estimated_on_disc_bytes=8,
            ),
        ],
    )

    assert planner_file.piece_count == 2


def test_planner_collection_payload_bytes_sums_plaintext_file_sizes() -> None:
    collection = PlannerCollection(
        collection_id="docs",
        files=[
            PlannerFile(
                file_id=1,
                relpath="/docs/a.txt",
                source=Path("/tmp/a.txt"),
                plaintext_bytes=12,
                mode=None,
                mtime=None,
                uid=None,
                gid=None,
                sha256="a" * 64,
            ),
            PlannerFile(
                file_id=2,
                relpath="/docs/b.txt",
                source=Path("/tmp/b.txt"),
                plaintext_bytes=30,
                mode=None,
                mtime=None,
                uid=None,
                gid=None,
                sha256="b" * 64,
            ),
        ],
    )

    assert collection.payload_bytes == 42
