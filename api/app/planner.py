from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
import numpy as np
from scipy.optimize import milp, Bounds, LinearConstraint
from scipy.sparse import lil_array
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import select

from .config import CONTAINER_STATE_DIR, CONTAINER_CFG, CONTAINER_ROOTS_DIR
from .crypto import encrypt_bytes_to_file, encrypt_file_span, encrypted_size_for_plaintext_size, logical_file_sha256_and_size
from .iso import estimate_iso_size_from_container_root
from .models import Container, ContainerEntry, ArchivePiece, Collection, CollectionFile
from .storage import canonical_tree_hash, inactive_collection_hash_manifest_path, inactive_collection_hash_proof_path, collection_container_artifact_relpaths

MANIFEST = "MANIFEST.yml"
README = "README.txt"
STORE = "files"
STATE = "state.json"
MANIFEST_SCHEMA = "manifest/v1"
# Reserve space for the encrypted manifest envelope plus the plaintext
# per-container README so planner-selected piece sets still fit when emitted.
META_PAD = 2048
MANIFEST_PLACEHOLDER_CONTAINER = "00000000T000000Z"
MANIFEST_PLACEHOLDER_CHUNK_COUNT = 999999
MANIFEST_PLACEHOLDER_ARCHIVE = "files/999999999999.999999"
_MISSING = object()


def atomic_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
    tmp.replace(path)


def load_state(state_dir: Path, cfg=None):
    p = state_dir / STATE
    if p.exists():
        s = json.loads(p.read_text())
        s.setdefault("collections", {})
        s.setdefault("items", [])
        s.setdefault("next_item", 0)
        s.setdefault("last_closed", "")
        return s
    if cfg is None:
        raise RuntimeError(f"no state at {state_dir}")
    s = {"cfg": cfg, "collections": {}, "items": [], "next_item": 0, "last_closed": ""}
    state_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(p, s)
    return s


def save_state(state_dir: Path, s):
    atomic_json(state_dir / STATE, s)


def ts_name(last: str = ""):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if last:
        prev = datetime.strptime(last, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if now <= prev:
            now = prev + timedelta(seconds=1)
    return now.strftime("%Y%m%dT%H%M%SZ")


def copy_span(src: str, dst: str, off: int = 0, size: int | None = None):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if size is None:
        size = os.path.getsize(src) - off
    if off == 0 and size == os.path.getsize(src):
        try:
            os.link(src, dst)
            return
        except OSError:
            shutil.copy2(src, dst)
            return
    with open(src, "rb") as s, open(dst, "wb") as d:
        s.seek(off)
        while size:
            b = s.read(min(1 << 20, size))
            if not b:
                break
            d.write(b)
            size -= len(b)


def sidecar_dict(f: dict, i: int = 0, n: int = 1):
    d = {
        "schema": "sidecar/v1",
        "path": f["rel"],
        "sha256": f["sha256"],
        "size": f["raw"],
        "mode": f["mode"],
        "mtime": f["mtime"],
    }
    if f["uid"] is not None:
        d["uid"] = f["uid"]
    if f["gid"] is not None:
        d["gid"] = f["gid"]
    if n > 1:
        d["part"] = {"index": i + 1, "count": n}
    return d


def sidecar_bytes(f: dict, i: int = 0, n: int = 1):
    return yaml.safe_dump(sidecar_dict(f, i, n), sort_keys=False, allow_unicode=True).encode()


def yaml_bytes(obj) -> bytes:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True).encode()


def manifest_file_entry(path: str, sha256: str, archive=_MISSING) -> dict:
    entry = {
        "path": path,
        "sha256": sha256,
    }
    if archive is not _MISSING:
        entry["archive"] = archive
    return entry


def manifest_dump(container: str, collections_payload: list[dict]) -> bytes:
    return yaml_bytes(
        {
            "schema": MANIFEST_SCHEMA,
            "container": container,
            "collections": collections_payload,
        }
    )


EMPTY_MANIFEST_SIZE = len(manifest_dump(MANIFEST_PLACEHOLDER_CONTAINER, []))
_BASELINE_FILE_ENTRY = manifest_file_entry(
    "placeholder",
    "0" * 64,
    {"count": MANIFEST_PLACEHOLDER_CHUNK_COUNT, "chunks": []},
)
_ONE_CHUNK_FILE_ENTRY = manifest_file_entry(
    "placeholder",
    "0" * 64,
    {"count": MANIFEST_PLACEHOLDER_CHUNK_COUNT, "chunks": [MANIFEST_PLACEHOLDER_ARCHIVE]},
)
_UNSPLIT_FILE_ENTRY = manifest_file_entry(
    "placeholder",
    "0" * 64,
    MANIFEST_PLACEHOLDER_ARCHIVE,
)
_BASELINE_FILE_SIZE = len(
    manifest_dump(
        MANIFEST_PLACEHOLDER_CONTAINER,
        [{"name": MANIFEST_PLACEHOLDER_CONTAINER, "files": [_BASELINE_FILE_ENTRY]}],
    )
)
_SPLIT_CHUNK_BUDGET = len(
    manifest_dump(
        MANIFEST_PLACEHOLDER_CONTAINER,
        [{"name": MANIFEST_PLACEHOLDER_CONTAINER, "files": [_ONE_CHUNK_FILE_ENTRY]}],
    )
) - _BASELINE_FILE_SIZE
_UNSPLIT_ARCHIVE_BUDGET = max(
    0,
    len(
        manifest_dump(
            MANIFEST_PLACEHOLDER_CONTAINER,
            [{"name": MANIFEST_PLACEHOLDER_CONTAINER, "files": [_UNSPLIT_FILE_ENTRY]}],
        )
    )
    - _BASELINE_FILE_SIZE,
)


def tree_plan(kids: dict, sizes: dict, cap: int):
    free, parts, stack = [cap], {}, [("", "dir")]
    while stack:
        node, why = stack.pop()
        if node not in kids or sizes[node] <= cap:
            idx = next((i for i, v in enumerate(free) if v >= sizes[node]), len(free))
            if idx == len(free):
                free.append(cap)
            free[idx] -= sizes[node]
            q = parts.setdefault(idx, {"pieces": [], "bytes": 0, "why": why})
            q["bytes"] += sizes[node]
            q.setdefault("nodes", []).append((node, why))
            continue
        order = sorted(kids[node], key=lambda x: (-sizes[x], str(x)))
        stack.extend((c, "spl") for c in reversed(order))
    return [parts[i] for i in sorted(parts)]


def manifest_collection_budget(collection: str, files: list[dict]) -> int:
    return len(
        manifest_dump(
            MANIFEST_PLACEHOLDER_CONTAINER,
            [
                {
                    "name": collection,
                    "files": [
                        manifest_file_entry(
                            f["rel"],
                            f["sha256"],
                            {"count": MANIFEST_PLACEHOLDER_CHUNK_COUNT, "chunks": []},
                        )
                        for f in sorted(files, key=lambda x: x["rel"])
                    ],
                }
            ],
        )
    ) - EMPTY_MANIFEST_SIZE


def put_len(collection: str, rel: str, i: int, n: int, why: str = "___"):
    return _SPLIT_CHUNK_BUDGET if n > 1 else _UNSPLIT_ARCHIVE_BUDGET


def stage_pieces(state_dir: Path, collection: str, files: list[dict], target: int, fixed: int, artifacts: list[dict] | None = None):
    artifacts = artifacts or []
    pool, cap = state_dir / "pool" / collection, target - META_PAD - fixed
    if cap <= 0:
        raise RuntimeError(f"collection manifest for {collection} leaves no payload room")
    pool.mkdir(parents=True, exist_ok=True)
    for f in files:
        sidecar_size = encrypted_size_for_plaintext_size(len(sidecar_bytes(f)))
        stub1 = put_len(collection, f["rel"], 0, 1, "collection")
        if f["raw"] <= target:
            if not _piece_fits_iso_target(state_dir, target, collection, files, f, 1, f["raw"], artifacts):
                raise RuntimeError(f"file {f['rel']} in {collection} cannot fit with required manifest overhead without forbidden chunking")
            store = pool / str(f["id"])
            encrypt_file_span(Path(f["src"]), store)
            stored_size = store.stat().st_size
            if stored_size + sidecar_size + stub1 > cap:
                raise RuntimeError(f"file {f['rel']} in {collection} cannot fit with required manifest overhead without forbidden chunking")
            f["pieces"] = [{"collection": collection, "rel": f["rel"], "file": f["id"], "store": str(store.relative_to(state_dir)), "data": f["raw"], "i": 0, "n": 1, "est": stored_size + sidecar_size + stub1}]
            continue
        n = 2
        while True:
            room = _max_piece_plaintext_size_for_iso_target(state_dir, target, collection, files, f, n, artifacts)
            if room <= 0:
                raise RuntimeError(f"chunk sidecar for {f['rel']} in {collection} leaves no payload room")
            nn = max(2, math.ceil(f["raw"] / room))
            if nn == n:
                break
            n = nn
        room, pcs, off = (_max_piece_plaintext_size_for_iso_target(state_dir, target, collection, files, f, n, artifacts), [], 0)
        w = max(3, len(str(n)))
        for i in range(n):
            b = min(room, f["raw"] - off)
            store = pool / f"{f['id']}.{i + 1:0{w}d}"
            encrypt_file_span(Path(f["src"]), store, off, b)
            sidecar_part_size = encrypted_size_for_plaintext_size(len(sidecar_bytes(f, i, n)))
            pcs.append(
                {
                    "collection": collection,
                    "rel": f["rel"],
                    "file": f["id"],
                    "store": str(store.relative_to(state_dir)),
                    "data": b,
                    "i": i,
                    "n": n,
                    "est": store.stat().st_size + sidecar_part_size + put_len(collection, f["rel"], i, n, "vol"),
                }
            )
            off += b
        f["pieces"] = pcs


def leaves(node, kids):
    stack = [node]
    while stack:
        n = stack.pop()
        if n not in kids:
            yield n
        else:
            stack.extend(reversed(kids[n]))


def split_collection(files: list[dict], kids: dict[str, list[str]], dirs: list[str], cap: int):
    kids, sizes, by_rel = ({k: v[:] for k, v in kids.items()}, {}, {f["rel"]: f for f in files})
    for f in files:
        sizes[f["rel"]] = sum(p["est"] for p in f["pieces"])
        if len(f["pieces"]) > 1:
            kids[f["rel"]] = [(f["rel"], p["i"]) for p in f["pieces"]]
        for p in f["pieces"]:
            sizes[(f["rel"], p["i"])] = p["est"]
    for d in reversed(dirs):
        sizes[d] = sum(sizes[c] for c in kids[d])
    by_leaf = {(f["rel"], p["i"]): (f, p) for f in files for p in f["pieces"]}
    parts = tree_plan(kids, sizes, cap)
    out = []
    for q in parts:
        cur = {"pieces": [], "bytes": 0, "why": q["why"]}
        for node, why in q.get("nodes", []):
            for leaf in leaves(node, kids):
                f, p = by_leaf[leaf] if leaf in by_leaf else (by_rel[leaf], by_rel[leaf]["pieces"][0])
                cur["pieces"].append({"collection": f["collection"], "file": f["id"], "rel": f["rel"], "store": p["store"], "data": p["data"], "i": p["i"], "n": p["n"]})
                cur["bytes"] += p["est"]
                if p["n"] > 1:
                    cur["why"] = "vol"
                elif why == "spl" and cur["why"] == "dir":
                    cur["why"] = "spl"
        out.append(cur)
    return out


def close_threshold(items: list[dict], fill: int, spill: int):
    return spill if any(x["priority"] for x in items) else fill


def _solve(items: list[dict], collections: dict, cap: int, fill: int, spill: int, force=False):
    js = sorted({x["collection"] for x in items})
    n, m = len(items), len(js)
    jix = {j: i for i, j in enumerate(js)}
    bytes_ = np.array([x["bytes"] for x in items], dtype=np.float64)
    priority = np.array([int(x["priority"]) for x in items], dtype=np.float64)
    fixed = np.array([collections[j]["fixed"] for j in js], dtype=np.float64)
    item_collection = np.array([jix[x["collection"]] for x in items], dtype=int)
    payload_cap = cap - META_PAD
    diff = fill - spill
    nv, ih = n + m + 1 + int(force), n + m
    idd = ih + 1 if force else None
    rows = 1 + n + m + int(priority.sum()) + 2 + int(not force) + 2 * int(force)
    A = lil_array((rows, nv), dtype=np.float64)
    lo, hi, r = [], [], 0

    A[r, :n] = bytes_
    A[r, n : n + m] = fixed
    lo += [-np.inf]
    hi += [payload_cap]
    r += 1
    for i, jv in enumerate(item_collection):
        A[r, i] = 1
        A[r, n + jv] = -1
        lo += [-np.inf]
        hi += [0]
        r += 1
    for jv in range(m):
        idx = np.where(item_collection == jv)[0]
        A[r, idx] = -1
        A[r, n + jv] = 1
        lo += [-np.inf]
        hi += [0]
        r += 1
    for i in np.where(priority > 0)[0]:
        A[r, i] = 1
        A[r, ih] = -1
        lo += [-np.inf]
        hi += [0]
        r += 1
    A[r, :n] = -priority
    A[r, ih] = 1
    lo += [-np.inf]
    hi += [0]
    r += 1
    A[r, :n] = 1
    lo += [1]
    hi += [np.inf]
    r += 1
    if force:
        A[r, :n] = bytes_
        A[r, n : n + m] = fixed
        A[r, idd] = -1
        lo += [-np.inf]
        hi += [fill - META_PAD]
        r += 1
        A[r, :n] = -bytes_
        A[r, n : n + m] = -fixed
        A[r, idd] = -1
        lo += [-np.inf]
        hi += [-(fill - META_PAD)]
        r += 1
    else:
        A[r, :n] = bytes_
        A[r, n : n + m] = fixed
        A[r, ih] = diff
        lo += [fill - META_PAD]
        hi += [np.inf]
        r += 1

    integrality = np.ones(nv, dtype=int)
    bounds_lo, bounds_hi = np.zeros(nv), np.ones(nv)
    if force:
        integrality[idd] = 0
        bounds_hi[idd] = np.inf
    cons = LinearConstraint(A.tocsr(), np.array(lo, dtype=np.float64), np.array(hi, dtype=np.float64))
    bounds = Bounds(bounds_lo, bounds_hi)

    def run(c, extra=()):
        if extra:
            aa = lil_array((len(extra), nv), dtype=np.float64)
            lo2, hi2 = [], []
            for rr, (vec, a, b) in enumerate(extra):
                aa[rr] = vec
                lo2.append(a)
                hi2.append(b)
            ec = LinearConstraint(aa.tocsr(), np.array(lo2, dtype=np.float64), np.array(hi2, dtype=np.float64))
            lc = (cons, ec)
        else:
            lc = cons
        res = milp(c=np.array(c, dtype=np.float64), constraints=lc, integrality=integrality, bounds=bounds, options={"mip_rel_gap": 0})
        if not res.success or res.x is None:
            return None
        x = np.rint(res.x).astype(int)
        used = int(META_PAD + bytes_ @ x[:n] + fixed @ x[n : n + m])
        selected = [items[i] for i, v in enumerate(x[:n]) if v]
        return {"x": x, "selected": selected, "used": used}

    if force:
        c1 = np.zeros(nv)
        c1[idd] = 1
        a = run(c1)
        if not a:
            return []
        d = int(round(a["x"][idd]))
        v1 = np.zeros(nv)
        v1[idd] = 1
        c2 = np.zeros(nv)
        c2[:n] = -bytes_
        c2[n : n + m] = -fixed
        b = run(c2, [(v1, d, d)])
        return b["selected"] if b else a["selected"]

    c1 = np.zeros(nv)
    c1[:n] = bytes_
    c1[n : n + m] = fixed
    c1[ih] = diff
    a = run(c1)
    if not a:
        return []
    s = a["used"] - close_threshold(a["selected"], fill, spill)
    v1 = np.zeros(nv)
    v1[:n] = bytes_
    v1[n : n + m] = fixed
    v1[ih] = diff
    c2 = np.zeros(nv)
    c2[:n] = -bytes_
    c2[n : n + m] = -fixed
    b = run(c2, [(v1, s, s)])
    if not b:
        return a["selected"]
    u = b["used"]
    v2 = np.zeros(nv)
    v2[:n] = bytes_
    v2[n : n + m] = fixed
    c3 = np.zeros(nv)
    c3[:n] = -priority
    c = run(c3, [(v1, s, s), (v2, u - META_PAD, u - META_PAD)])
    return c["selected"] if c else b["selected"]


def pick(items: list[dict], collections: dict, cap: int, fill: int, spill: int, force=False):
    return _solve(items, collections, cap, fill, spill, force) if items else []


def assign_paths(pieces: list[dict]):
    files = sorted({(p["collection"], p["file"], p["rel"]) for p in pieces}, key=lambda x: (x[0], x[2], str(x[1])))
    base = {(j, fid): i for i, (j, fid, _) in enumerate(files)}
    out = {}
    for p in pieces:
        k, w = base[(p["collection"], p["file"])], max(3, len(str(p["n"])))
        f = f"{STORE}/{k}" + ("" if p["n"] == 1 else f".{p['i'] + 1:0{w}d}")
        out[(p["collection"], p["file"], p["i"])] = (f, f + ".meta.yaml")
    return out


def manifest_bytes(part: str, cfg: dict, collections: dict, items: list[dict], fmap: dict):
    pieces_by_file: dict[tuple[str, int], list[dict]] = {}
    for item in items:
        for piece in item["pieces"]:
            pieces_by_file.setdefault((piece["collection"], piece["file"]), []).append(piece)

    manifest_collections: list[dict] = []
    for collection in sorted({x["collection"] for x in items}):
        collection_files = []
        for file_meta in sorted(collections[collection]["files"], key=lambda x: x["rel"]):
            present = sorted(
                pieces_by_file.get((collection, file_meta["id"]), []),
                key=lambda x: x["i"],
            )
            if file_meta["piece_count"] > 1:
                archive = {
                    "count": file_meta["piece_count"],
                    "chunks": [fmap[(collection, file_meta["id"], piece["i"])][0] for piece in present],
                }
                collection_files.append(manifest_file_entry(file_meta["rel"], file_meta["sha256"], archive))
            elif present:
                collection_files.append(
                    manifest_file_entry(
                        file_meta["rel"],
                        file_meta["sha256"],
                        fmap[(collection, file_meta["id"], 0)][0],
                    )
                )
            else:
                collection_files.append(manifest_file_entry(file_meta["rel"], file_meta["sha256"]))
        manifest_collections.append({"name": collection, "files": collection_files})

    return manifest_dump(part, manifest_collections)


def recovery_readme_bytes(part: str) -> bytes:
    lines = [
        f"Archive container: {part}",
        "",
        "This README.txt is intentionally plaintext. Every other leaf file on this container is age-encrypted.",
        "",
        "Requirements:",
        "- age CLI with age-plugin-batchpass in PATH",
        "- the archive passphrase used when this container was created",
        "",
        "Set the passphrase in your shell:",
        "  export AGE_PASSPHRASE='your-passphrase'",
        "",
        "Decrypt the manifest for this container:",
        "  age -d -j batchpass MANIFEST.yml > MANIFEST.dec.yml",
        "",
        "The decrypted manifest is manifest/v1 YAML:",
        "- schema: manifest/v1",
        "- collections[*].files[*].archive is omitted when an unsplit file has no payload on this container",
        "- archive is a string for an unsplit payload on this container",
        "- archive.count and archive.chunks describe split payloads present on this container",
        "",
        "Decrypt a payload:",
        "  age -d -j batchpass files/<entry> > recovered.bin",
        "",
        "Decrypt a sidecar for any payload listed above:",
        "  age -d -j batchpass files/<entry>.meta.yaml > files/<entry>.meta.dec.yaml",
        "",
        "Per-collection hash manifests and OpenTimestamps proofs are stored under collections/<collection>/ on any container carrying that collection.",
        "",
        "For split files, gather every chunk from every required container and concatenate them in chunk-index order.",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _piece_manifest_bytes(collection: str, files: list[dict], target_file_id: int, n: int):
    manifest_files = []
    for file_meta in sorted(files, key=lambda x: x["rel"]):
        archive = _MISSING
        if file_meta["id"] == target_file_id:
            archive = (
                MANIFEST_PLACEHOLDER_ARCHIVE
                if n == 1
                else {"count": n, "chunks": [MANIFEST_PLACEHOLDER_ARCHIVE]}
            )
        manifest_files.append(manifest_file_entry(file_meta["rel"], file_meta["sha256"], archive))
    return manifest_dump(
        MANIFEST_PLACEHOLDER_CONTAINER,
        [{"name": collection, "files": manifest_files}],
    )


def _piece_fits_iso_target(
    state_dir: Path,
    target: int,
    collection: str,
    files: list[dict],
    file_meta: dict,
    n: int,
    payload_plaintext_size: int,
    artifacts: list[dict],
) -> bool:
    manifest_size = encrypted_size_for_plaintext_size(len(_piece_manifest_bytes(collection, files, file_meta["id"], n)))
    sidecar_size = encrypted_size_for_plaintext_size(len(sidecar_bytes(file_meta, 0, n)))
    payload_size = encrypted_size_for_plaintext_size(payload_plaintext_size)
    readme_size = len(recovery_readme_bytes(MANIFEST_PLACEHOLDER_CONTAINER))
    placeholder_entries = [
        {"relpath": MANIFEST, "size": manifest_size},
        {"relpath": README, "size": readme_size},
        {"relpath": MANIFEST_PLACEHOLDER_ARCHIVE, "size": payload_size},
        {"relpath": f"{MANIFEST_PLACEHOLDER_ARCHIVE}.meta.yaml", "size": sidecar_size},
        *(
            {"relpath": artifact["container_relpath"], "size": artifact["encrypted_size"]}
            for artifact in artifacts
        ),
    ]
    fallback = sum(entry["size"] for entry in placeholder_entries)
    with tempfile.TemporaryDirectory(prefix=".piece-preview-", dir=state_dir) as tmp_dir:
        root = Path(tmp_dir) / MANIFEST_PLACEHOLDER_CONTAINER
        for entry in placeholder_entries:
            _write_placeholder_file(root / entry["relpath"], entry["size"])
        used = _estimate_iso_size_bytes(root, MANIFEST_PLACEHOLDER_CONTAINER, fallback)
    return used <= target


def _max_piece_plaintext_size_for_iso_target(
    state_dir: Path,
    target: int,
    collection: str,
    files: list[dict],
    file_meta: dict,
    n: int,
    artifacts: list[dict],
) -> int:
    low, high = 0, file_meta["raw"]
    while low < high:
        mid = (low + high + 1) // 2
        if _piece_fits_iso_target(state_dir, target, collection, files, file_meta, n, mid, artifacts):
            low = mid
        else:
            high = mid - 1
    return low


def _build_container_layout(state_dir: Path, s: dict, items: list[dict], part: str) -> dict:
    pieces = [p for it in items for p in it["pieces"]]
    collections_on_container = sorted({x["collection"] for x in items})
    fmap = assign_paths(pieces)
    man = manifest_bytes(part, s["cfg"], s["collections"], items, fmap)
    readme = recovery_readme_bytes(part)
    payload_entries: list[dict] = [
        {
            "kind": "manifest",
            "relpath": MANIFEST,
            "size": encrypted_size_for_plaintext_size(len(man)),
            "plaintext": man,
        },
        {
            "kind": "readme",
            "relpath": README,
            "size": len(readme),
            "plaintext": readme,
        },
    ]
    piece_records: list[dict] = []
    for it in items:
        meta = {f["id"]: f for f in s["collections"][it["collection"]]["files"]}
        for p in it["pieces"]:
            payload_relpath, sidecar_relpath = fmap[(p["collection"], p["file"], p["i"])]
            payload_entries.append(
                {
                    "kind": "payload",
                    "relpath": payload_relpath,
                    "size": (state_dir / p["store"]).stat().st_size,
                    "source": state_dir / p["store"],
                }
            )
            sidecar_plaintext = sidecar_bytes(meta[p["file"]], p["i"], p["n"])
            payload_entries.append(
                {
                    "kind": "sidecar",
                    "relpath": sidecar_relpath,
                    "size": encrypted_size_for_plaintext_size(len(sidecar_plaintext)),
                    "plaintext": sidecar_plaintext,
                }
            )
            piece_records.append(
                {
                    "collection": p["collection"],
                    "collection_file_id": p["file"],
                    "relative_path": p["rel"],
                    "payload_relpath": payload_relpath,
                    "sidecar_relpath": sidecar_relpath,
                    "payload_size_bytes": p["data"],
                    "chunk_index": None if p["n"] == 1 else p["i"] + 1,
                    "chunk_count": None if p["n"] == 1 else p["n"],
                }
            )
    for collection_id in collections_on_container:
        for artifact in s["collections"][collection_id]["artifacts"]:
            payload_entries.append(
                {
                    "kind": "artifact",
                    "relpath": artifact["container_relpath"],
                    "size": artifact["encrypted_size"],
                    "source": Path(artifact["source"]),
                }
            )
    return {
        "collections": collections_on_container,
        "items": [x["id"] for x in items],
        "entries": payload_entries,
        "pieces": piece_records,
        "root_used": sum(entry["size"] for entry in payload_entries),
    }


def _write_placeholder_file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(size)


def _materialize_preview_root(root: Path, layout: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for entry in layout["entries"]:
        _write_placeholder_file(root / entry["relpath"], entry["size"])


def _materialize_container_root(root: Path, layout: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for entry in layout["entries"]:
        dest = root / entry["relpath"]
        kind = entry["kind"]
        if kind == "readme":
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(entry["plaintext"])
        elif kind in {"manifest", "sidecar"}:
            encrypt_bytes_to_file(entry["plaintext"], dest)
        elif kind == "artifact":
            encrypt_bytes_to_file(Path(entry["source"]).read_bytes(), dest)
        elif kind == "payload":
            copy_span(str(entry["source"]), str(dest))
        else:
            raise RuntimeError(f"unsupported container entry kind {kind}")


def _estimate_iso_size_bytes(root: Path, part: str, fallback: int) -> int:
    try:
        return estimate_iso_size_from_container_root(root, requested_label=part)
    except RuntimeError as exc:
        if str(exc).endswith("is not installed"):
            return fallback
        raise


def _container_result(part: str, out_dir: Path, target: int, layout: dict, used: int) -> dict:
    return {
        "name": part,
        "path": str((out_dir / part).resolve()),
        "used": used,
        "root_used": layout["root_used"],
        "iso_overhead": used - layout["root_used"],
        "free": target - used,
        "collections": layout["collections"],
        "items": layout["items"],
        "pieces": layout["pieces"],
    }


def preview_container(state_dir: Path, s: dict, items: list[dict], out_dir: Path, part: str | None = None) -> dict:
    part = part or ts_name(s.get("last_closed", ""))
    layout = _build_container_layout(state_dir, s, items, part)
    with tempfile.TemporaryDirectory(prefix=".preview-", dir=out_dir) as tmp_dir:
        root = Path(tmp_dir) / part
        _materialize_preview_root(root, layout)
        used = _estimate_iso_size_bytes(root, part, layout["root_used"])
    return _container_result(part, out_dir, s["cfg"]["target"], layout, used)


def build_container(state_dir: Path, s: dict, items: list[dict], out_dir: Path, emit=True, part: str | None = None):
    if not emit:
        return preview_container(state_dir, s, items, out_dir, part)
    part = part or ts_name(s.get("last_closed", ""))
    layout = _build_container_layout(state_dir, s, items, part)
    root = out_dir / part
    if root.exists():
        shutil.rmtree(root)
    _materialize_container_root(root, layout)
    used = _estimate_iso_size_bytes(root, part, layout["root_used"])
    if used > s["cfg"]["target"]:
        shutil.rmtree(root, ignore_errors=True)
        return None
    out = _container_result(part, out_dir, s["cfg"]["target"], layout, used)
    s["last_closed"] = part
    return out


def gc_state(state_dir: Path, s: dict, done: list[dict]):
    dead = {x["id"] for x in done}
    for it in [x for x in s["items"] if x["id"] in dead]:
        for p in it["pieces"]:
            try:
                (state_dir / p["store"]).unlink()
            except FileNotFoundError:
                pass
    s["items"] = [x for x in s["items"] if x["id"] not in dead]
    live = {x["collection"] for x in s["items"]}
    for collection in list(s["collections"]):
        if collection not in live:
            shutil.rmtree(state_dir / "pool" / collection, ignore_errors=True)
            del s["collections"][collection]


def fit_candidate_to_target(state_dir: Path, s: dict, items: list[dict], out_dir: Path, part: str, force=False):
    target = s["cfg"]["target"]
    seen: set[tuple[str, ...]] = set()
    cand = items
    while cand:
        key = tuple(x["id"] for x in cand)
        if key in seen:
            cand = cand[:-1]
            continue
        seen.add(key)
        preview = preview_container(state_dir, s, cand, out_dir, part)
        if preview["used"] <= target:
            return cand, preview
        overshoot = preview["used"] - target
        next_cap = preview["root_used"] - max(1, overshoot)
        if next_cap > META_PAD:
            alt = pick(s["items"], s["collections"], next_cap, s["cfg"]["fill"], s["cfg"]["spill_fill"], force)
            if alt and tuple(x["id"] for x in alt) not in seen:
                cand = alt
                continue
        cand = cand[:-1]
    return [], None


def flush(state_dir: Path, s: dict, out_dir: Path, force=False):
    out = []
    while True:
        cand = pick(s["items"], s["collections"], s["cfg"]["target"], s["cfg"]["fill"], s["cfg"]["spill_fill"], force)
        forced = force
        if not cand and sum(x["bytes"] for x in s["items"]) > s["cfg"]["buffer_max"] and s["items"]:
            cand, forced = (pick(s["items"], s["collections"], s["cfg"]["target"], s["cfg"]["fill"], s["cfg"]["spill_fill"], True), True)
        if not cand:
            break
        part = ts_name(s.get("last_closed", ""))
        made = None
        while cand:
            cand, preview = fit_candidate_to_target(state_dir, s, cand, out_dir, part, forced)
            if not cand or preview is None:
                break
            req = close_threshold(cand, s["cfg"]["fill"], s["cfg"]["spill_fill"])
            if not forced and preview["used"] < req:
                cand = []
                break
            made = build_container(state_dir, s, cand, out_dir, True, part)
            if made is not None:
                break
            cand = cand[:-1]
        if made is None:
            break
        out.append(made)
        gc_state(state_dir, s, cand)
    return out


def build_collection_structures(collection: Collection):
    files = []
    kids: dict[str, list] = {"": []}
    dirs = {""}
    explicit_dirs = {d.relative_path for d in collection.directories}
    for d in explicit_dirs:
        parts = d.split("/")
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    for jf in sorted(collection.files, key=lambda x: x.relative_path):
        if not jf.buffer_abs_path or not Path(jf.buffer_abs_path).exists():
            raise RuntimeError(f"collection file {jf.relative_path} is not present in active buffer")
        rel = jf.relative_path
        for parent in [""] + ["/".join(rel.split("/")[:i]) for i in range(1, len(rel.split("/")) )]:
            dirs.add(parent)
        files.append({"id": jf.id, "collection": collection.id, "src": jf.buffer_abs_path, "rel": rel, "raw": jf.size_bytes, "mode": jf.mode, "mtime": jf.mtime, "uid": jf.uid, "gid": jf.gid, "sha256": jf.actual_sha256 or jf.expected_sha256})
    for d in sorted(dirs, key=lambda x: (x.count("/"), x)):
        kids.setdefault(d, [])
    for d in sorted(dirs):
        if not d:
            continue
        parent = "/".join(d.split("/")[:-1])
        kids.setdefault(parent, [])
        if d not in kids[parent]:
            kids[parent].append(d)
    for f in files:
        parent = "/".join(f["rel"].split("/")[:-1])
        kids.setdefault(parent, [])
        kids[parent].append(f["rel"])
    for values in kids.values():
        values.sort()
    dir_list = sorted(dirs, key=lambda x: (x.count("/"), x))
    return files, kids, dir_list


def ingest_collection(session: Session, collection_id: str):
    collection = session.execute(select(Collection).where(Collection.id == collection_id).options(selectinload(Collection.directories), selectinload(Collection.files))).scalar_one()
    if collection.status == "sealed":
        raise RuntimeError(f"collection {collection_id} already sealed")
    files, kids, dirs = build_collection_structures(collection)
    state_dir = CONTAINER_STATE_DIR
    out_dir = CONTAINER_ROOTS_DIR
    s = load_state(state_dir, CONTAINER_CFG)
    if s["cfg"] != CONTAINER_CFG:
        raise RuntimeError("container state config mismatch")
    if collection_id in s["collections"] or any(x["collection"] == collection_id for x in s["items"]):
        raise RuntimeError(f"duplicate collection {collection_id}")
    if not files:
        collection.status = "sealed"
        collection.sealed_at = datetime.now(timezone.utc)
        save_state(state_dir, s)
        return {"collection": collection_id, "closed": [], "buffer_bytes": sum(x["bytes"] for x in s["items"])}

    manifest_artifact = inactive_collection_hash_manifest_path(collection_id)
    proof_artifact = inactive_collection_hash_proof_path(collection_id)
    if not manifest_artifact.exists() or not proof_artifact.exists():
        raise RuntimeError(f"collection hash artifacts are missing for {collection_id}")
    manifest_relpath, proof_relpath = collection_container_artifact_relpaths(collection_id)
    artifacts = [
        {
            "source": str(manifest_artifact),
            "container_relpath": manifest_relpath,
            "encrypted_size": encrypted_size_for_plaintext_size(manifest_artifact.stat().st_size),
        },
        {
            "source": str(proof_artifact),
            "container_relpath": proof_relpath,
            "encrypted_size": encrypted_size_for_plaintext_size(proof_artifact.stat().st_size),
        },
    ]

    fixed = manifest_collection_budget(collection_id, files) + sum(item["encrypted_size"] for item in artifacts)
    stage_pieces(state_dir, collection_id, files, s["cfg"]["target"], fixed, artifacts)
    s["collections"][collection_id] = {
        "files": [
            {
                **{k: f[k] for k in ("id", "rel", "raw", "mode", "mtime", "uid", "gid", "sha256")},
                "piece_count": len(f["pieces"]),
            }
            for f in files
        ],
        "artifacts": artifacts,
        "fixed": fixed,
    }

    def add_item(kind, priority, why, pieces, b):
        s["next_item"] += 1
        s["items"].append({"id": f"{s['next_item']:08d}", "collection": collection_id, "kind": kind, "priority": priority, "why": why, "pieces": pieces, "bytes": b})

    total = META_PAD + fixed + sum(p["est"] for f in files for p in f["pieces"])
    if total <= s["cfg"]["target"] and all(f["raw"] <= s["cfg"]["target"] for f in files):
        add_item("collection", False, "collection", [{"collection": collection_id, "file": p["file"], "rel": p["rel"], "store": p["store"], "data": p["data"], "i": p["i"], "n": p["n"]} for f in files for p in f["pieces"]], sum(p["est"] for f in files for p in f["pieces"]))
    else:
        for q in split_collection(files, kids, dirs, s["cfg"]["target"] - META_PAD - fixed):
            add_item("rem", True, q["why"], q["pieces"], q["bytes"])
    closed = flush(state_dir, s, out_dir)
    collection.status = "sealed"
    collection.sealed_at = datetime.now(timezone.utc)
    save_state(state_dir, s)
    return {"collection": collection_id, "closed": closed, "buffer_bytes": sum(x["bytes"] for x in s["items"])}


def force_close_pending(session: Session):
    state_dir = CONTAINER_STATE_DIR
    out_dir = CONTAINER_ROOTS_DIR
    s = load_state(state_dir, CONTAINER_CFG)
    closed = flush(state_dir, s, out_dir, force=True)
    save_state(state_dir, s)
    return closed


def import_closed_containers(session: Session, closed: list[dict]) -> list[str]:
    from .notifications import create_container_finalization_notifications_for_container

    container_ids: list[str] = []
    for item in closed:
        container_id = item["name"]
        root = Path(item["path"])
        contents_hash, total_bytes, rows = canonical_tree_hash(root)
        container = session.get(Container, container_id)
        if container is None:
            container = Container(id=container_id, status="inactive", root_abs_path=str(root), contents_hash=contents_hash, total_root_bytes=total_bytes)
            session.add(container)
        else:
            container.root_abs_path = str(root)
            container.contents_hash = contents_hash
            container.total_root_bytes = total_bytes
        session.flush()
        session.query(ContainerEntry).filter(ContainerEntry.container_id == container_id).delete()
        session.query(ArchivePiece).filter(ArchivePiece.container_id == container_id).delete()
        for row in rows:
            kind = "payload"
            rel = row["relative_path"]
            if rel == MANIFEST:
                kind = "manifest"
            elif rel == README:
                kind = "readme"
            elif str(rel).endswith(".meta.yaml"):
                kind = "sidecar"
            elif str(rel).startswith("collections/") and str(rel).endswith("/HASHES.yml"):
                kind = "collection_hash_manifest"
            elif str(rel).startswith("collections/") and str(rel).endswith("/HASHES.yml.ots"):
                kind = "collection_hash_proof"
            logical_sha256, logical_size = logical_file_sha256_and_size(root / rel)
            session.add(
                ContainerEntry(
                    container_id=container_id,
                    relative_path=str(rel),
                    kind=kind,
                    size_bytes=logical_size,
                    sha256=logical_sha256,
                    stored_size_bytes=int(row["size_bytes"]),
                    stored_sha256=str(row["sha256"]),
                )
            )
        for p in item.get("pieces", []):
            session.add(ArchivePiece(container_id=container_id, collection_file_id=p["collection_file_id"], payload_relpath=p["payload_relpath"], sidecar_relpath=p["sidecar_relpath"], payload_size_bytes=p["payload_size_bytes"], chunk_index=p["chunk_index"], chunk_count=p["chunk_count"]))
        create_container_finalization_notifications_for_container(session, container_id)
        container_ids.append(container_id)
    session.commit()
    return container_ids
