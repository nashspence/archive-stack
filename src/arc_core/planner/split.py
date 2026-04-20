from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def tree_plan(children: dict[str, list[object]], sizes: dict[object, int], cap: int) -> list[dict[str, object]]:
    free = [cap]
    parts: dict[int, dict[str, object]] = {}
    stack: list[tuple[object, str]] = [("", "dir")]
    while stack:
        node, reason = stack.pop()
        if node not in children or sizes[node] <= cap:
            index = next((idx for idx, available in enumerate(free) if available >= sizes[node]), len(free))
            if index == len(free):
                free.append(cap)
            free[index] -= sizes[node]
            bucket = parts.setdefault(index, {"pieces": [], "bytes": 0, "reason": reason, "nodes": []})
            bucket["bytes"] = int(bucket["bytes"]) + sizes[node]
            cast_nodes = bucket.setdefault("nodes", [])
            assert isinstance(cast_nodes, list)
            cast_nodes.append((node, reason))
            continue
        ordered = sorted(children[node], key=lambda item: (-sizes[item], str(item)))
        stack.extend((child, "split") for child in reversed(ordered))
    return [parts[index] for index in sorted(parts)]



def leaves(node: object, children: dict[object, list[object]]) -> Iterable[object]:
    stack = [node]
    while stack:
        current = stack.pop()
        if current not in children:
            yield current
        else:
            stack.extend(reversed(children[current]))



def split_collection(
    *,
    files: list[dict[str, Any]],
    children: dict[str, list[str]],
    directories: list[str],
    cap: int,
) -> list[dict[str, object]]:
    mutable_children: dict[object, list[object]] = {key: value[:] for key, value in children.items()}
    sizes: dict[object, int] = {}
    by_rel = {file_meta["relpath"]: file_meta for file_meta in files}

    for file_meta in files:
        sizes[file_meta["relpath"]] = sum(piece["estimated_on_disc_bytes"] for piece in file_meta["pieces"])
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
    output: list[dict[str, object]] = []
    for part in planned:
        current: dict[str, object] = {"pieces": [], "bytes": 0, "reason": part["reason"]}
        for node, reason in part.get("nodes", []):
            for leaf in leaves(node, mutable_children):
                if leaf in by_leaf:
                    file_meta, piece = by_leaf[leaf]
                else:
                    file_meta = by_rel[str(leaf)]
                    piece = file_meta["pieces"][0]
                cast_pieces = current["pieces"]
                assert isinstance(cast_pieces, list)
                cast_pieces.append(piece)
                current["bytes"] = int(current["bytes"]) + int(piece["estimated_on_disc_bytes"])
                if piece["piece_count"] > 1:
                    current["reason"] = "volume"
                elif reason == "split" and current["reason"] == "dir":
                    current["reason"] = "split"
        output.append(current)
    return output
