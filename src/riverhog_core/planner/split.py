from __future__ import annotations

from collections.abc import Iterable
from typing import TypedDict, cast

NodeKey = str | tuple[str, int]


class SplitPiece(TypedDict):
    piece_index: int
    piece_count: int
    estimated_on_disc_bytes: int


class SplitFileMeta(TypedDict):
    relpath: str
    pieces: list[SplitPiece]


class TreePlanPart(TypedDict):
    pieces: list[object]
    bytes: int
    reason: str
    nodes: list[tuple[NodeKey, str]]


class SplitPlanPart(TypedDict):
    pieces: list[SplitPiece]
    bytes: int
    reason: str


def tree_plan(
    children: dict[NodeKey, list[NodeKey]], sizes: dict[NodeKey, int], cap: int
) -> list[TreePlanPart]:
    free = [cap]
    parts: dict[int, TreePlanPart] = {}
    stack: list[tuple[NodeKey, str]] = [("", "dir")]
    while stack:
        node, reason = stack.pop()
        if node not in children or sizes[node] <= cap:
            index = next(
                (idx for idx, available in enumerate(free) if available >= sizes[node]), len(free)
            )
            if index == len(free):
                free.append(cap)
            free[index] -= sizes[node]
            bucket = parts.setdefault(
                index, {"pieces": [], "bytes": 0, "reason": reason, "nodes": []}
            )
            bucket["bytes"] += sizes[node]
            bucket["nodes"].append((node, reason))
            continue
        ordered = sorted(children[node], key=lambda item: (-sizes[item], str(item)))
        stack.extend((child, "split") for child in reversed(ordered))
    return [parts[index] for index in sorted(parts)]


def leaves(node: NodeKey, children: dict[NodeKey, list[NodeKey]]) -> Iterable[NodeKey]:
    stack = [node]
    while stack:
        current = stack.pop()
        if current not in children:
            yield current
        else:
            stack.extend(reversed(children[current]))


def split_collection(
    *,
    files: list[SplitFileMeta],
    children: dict[str, list[str]],
    directories: list[str],
    cap: int,
) -> list[SplitPlanPart]:
    mutable_children: dict[NodeKey, list[NodeKey]] = {
        key: [cast(NodeKey, child) for child in value] for key, value in children.items()
    }
    sizes: dict[NodeKey, int] = {}
    by_rel = {file_meta["relpath"]: file_meta for file_meta in files}

    for file_meta in files:
        sizes[file_meta["relpath"]] = sum(
            piece["estimated_on_disc_bytes"] for piece in file_meta["pieces"]
        )
        if len(file_meta["pieces"]) > 1:
            mutable_children[file_meta["relpath"]] = [
                (file_meta["relpath"], piece["piece_index"]) for piece in file_meta["pieces"]
            ]
        for piece in file_meta["pieces"]:
            sizes[(file_meta["relpath"], piece["piece_index"])] = piece["estimated_on_disc_bytes"]

    for directory in reversed(directories):
        sizes[directory] = sum(sizes[child] for child in mutable_children[directory])

    by_leaf = {
        (file_meta["relpath"], piece["piece_index"]): (file_meta, piece)
        for file_meta in files
        for piece in file_meta["pieces"]
    }

    planned = tree_plan(mutable_children, sizes, cap)
    output: list[SplitPlanPart] = []
    for part in planned:
        current: SplitPlanPart = {"pieces": [], "bytes": 0, "reason": part["reason"]}
        for node, reason in part["nodes"]:
            for leaf in leaves(node, mutable_children):
                if leaf in by_leaf:
                    file_meta, piece = by_leaf[leaf]
                else:
                    file_meta = by_rel[str(leaf)]
                    piece = file_meta["pieces"][0]
                current["pieces"].append(piece)
                current["bytes"] += piece["estimated_on_disc_bytes"]
                if piece["piece_count"] > 1:
                    current["reason"] = "volume"
                elif reason == "split" and current["reason"] == "dir":
                    current["reason"] = "split"
        output.append(current)
    return output
