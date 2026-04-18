from __future__ import annotations

import json
import math
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
import numpy as np
from scipy.optimize import milp, Bounds, LinearConstraint
from scipy.sparse import lil_array
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import select

from .config import PARTITIONER_STATE_DIR, PARTITION_CFG, PARTITION_ROOTS_DIR
from .crypto import encrypt_bytes_to_file, encrypt_file_span, encrypted_size_for_plaintext_size, logical_file_sha256_and_size, max_plaintext_size_for_encrypted_budget
from .models import Disc, DiscEntry, ArchivePiece, Job, JobFile
from .storage import canonical_tree_hash, cold_job_hash_manifest_path, cold_job_hash_proof_path, job_disc_artifact_relpaths

MANIFEST = "MANIFEST.yml"
README = "README.txt"
STORE = "files"
STATE = "state.json"
MANIFEST_SCHEMA = "manifest/v1"
# Reserve space for the encrypted manifest envelope plus the plaintext
# per-disc README so planner-selected piece sets still fit when emitted.
META_PAD = 2048
MANIFEST_PLACEHOLDER_PARTITION = "00000000T000000Z"
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
        s.setdefault("jobs", {})
        s.setdefault("items", [])
        s.setdefault("next_item", 0)
        s.setdefault("last_closed", "")
        return s
    if cfg is None:
        raise RuntimeError(f"no state at {state_dir}")
    s = {"cfg": cfg, "jobs": {}, "items": [], "next_item": 0, "last_closed": ""}
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


def manifest_dump(partition: str, jobs_payload: list[dict]) -> bytes:
    return yaml_bytes(
        {
            "schema": MANIFEST_SCHEMA,
            "partition": partition,
            "jobs": jobs_payload,
        }
    )


EMPTY_MANIFEST_SIZE = len(manifest_dump(MANIFEST_PLACEHOLDER_PARTITION, []))
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
        MANIFEST_PLACEHOLDER_PARTITION,
        [{"name": MANIFEST_PLACEHOLDER_PARTITION, "files": [_BASELINE_FILE_ENTRY]}],
    )
)
_SPLIT_CHUNK_BUDGET = len(
    manifest_dump(
        MANIFEST_PLACEHOLDER_PARTITION,
        [{"name": MANIFEST_PLACEHOLDER_PARTITION, "files": [_ONE_CHUNK_FILE_ENTRY]}],
    )
) - _BASELINE_FILE_SIZE
_UNSPLIT_ARCHIVE_BUDGET = max(
    0,
    len(
        manifest_dump(
            MANIFEST_PLACEHOLDER_PARTITION,
            [{"name": MANIFEST_PLACEHOLDER_PARTITION, "files": [_UNSPLIT_FILE_ENTRY]}],
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


def manifest_job_budget(job: str, files: list[dict]) -> int:
    return len(
        manifest_dump(
            MANIFEST_PLACEHOLDER_PARTITION,
            [
                {
                    "name": job,
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


def put_len(job: str, rel: str, i: int, n: int, why: str = "___"):
    return _SPLIT_CHUNK_BUDGET if n > 1 else _UNSPLIT_ARCHIVE_BUDGET


def stage_pieces(state_dir: Path, job: str, files: list[dict], target: int, fixed: int):
    pool, cap = state_dir / "pool" / job, target - META_PAD - fixed
    if cap <= 0:
        raise RuntimeError(f"job manifest for {job} leaves no payload room")
    pool.mkdir(parents=True, exist_ok=True)
    for f in files:
        sidecar_size = encrypted_size_for_plaintext_size(len(sidecar_bytes(f)))
        stub1 = put_len(job, f["rel"], 0, 1, "job")
        if f["raw"] <= target:
            store = pool / str(f["id"])
            encrypt_file_span(Path(f["src"]), store)
            stored_size = store.stat().st_size
            if stored_size + sidecar_size + stub1 > cap:
                raise RuntimeError(f"file {f['rel']} in {job} cannot fit with required manifest overhead without forbidden chunking")
            f["pieces"] = [{"job": job, "rel": f["rel"], "file": f["id"], "store": str(store.relative_to(state_dir)), "data": f["raw"], "i": 0, "n": 1, "est": stored_size + sidecar_size + stub1}]
            continue
        n = max(
            2,
            math.ceil(
                f["raw"] / max(
                    1,
                    max_plaintext_size_for_encrypted_budget(
                        cap
                        - encrypted_size_for_plaintext_size(len(sidecar_bytes(f, 0, 2)))
                        - put_len(job, f["rel"], 0, 2, "vol")
                    ),
                )
            ),
        )
        while True:
            room = max_plaintext_size_for_encrypted_budget(
                cap - encrypted_size_for_plaintext_size(len(sidecar_bytes(f, 0, n))) - put_len(job, f["rel"], 0, n, "vol")
            )
            if room <= 0:
                raise RuntimeError(f"chunk sidecar for {f['rel']} in {job} leaves no payload room")
            nn = max(2, math.ceil(f["raw"] / room))
            if nn == n:
                break
            n = nn
        room, pcs, off = (
            max_plaintext_size_for_encrypted_budget(
                cap - encrypted_size_for_plaintext_size(len(sidecar_bytes(f, 0, n))) - put_len(job, f["rel"], 0, n, "vol")
            ),
            [],
            0,
        )
        w = max(3, len(str(n)))
        for i in range(n):
            b = min(room, f["raw"] - off)
            store = pool / f"{f['id']}.{i + 1:0{w}d}"
            encrypt_file_span(Path(f["src"]), store, off, b)
            sidecar_part_size = encrypted_size_for_plaintext_size(len(sidecar_bytes(f, i, n)))
            pcs.append(
                {
                    "job": job,
                    "rel": f["rel"],
                    "file": f["id"],
                    "store": str(store.relative_to(state_dir)),
                    "data": b,
                    "i": i,
                    "n": n,
                    "est": store.stat().st_size + sidecar_part_size + put_len(job, f["rel"], i, n, "vol"),
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


def split_job(files: list[dict], kids: dict[str, list[str]], dirs: list[str], cap: int):
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
                cur["pieces"].append({"job": f["job"], "file": f["id"], "rel": f["rel"], "store": p["store"], "data": p["data"], "i": p["i"], "n": p["n"]})
                cur["bytes"] += p["est"]
                if p["n"] > 1:
                    cur["why"] = "vol"
                elif why == "spl" and cur["why"] == "dir":
                    cur["why"] = "spl"
        out.append(cur)
    return out


def close_threshold(items: list[dict], fill: int, spill: int):
    return spill if any(x["hot"] for x in items) else fill


def _solve(items: list[dict], jobs: dict, cap: int, fill: int, spill: int, force=False):
    js = sorted({x["job"] for x in items})
    n, m = len(items), len(js)
    jix = {j: i for i, j in enumerate(js)}
    bytes_ = np.array([x["bytes"] for x in items], dtype=np.float64)
    hot = np.array([int(x["hot"]) for x in items], dtype=np.float64)
    fixed = np.array([jobs[j]["fixed"] for j in js], dtype=np.float64)
    item_job = np.array([jix[x["job"]] for x in items], dtype=int)
    payload_cap = cap - META_PAD
    diff = fill - spill
    nv, ih = n + m + 1 + int(force), n + m
    idd = ih + 1 if force else None
    rows = 1 + n + m + int(hot.sum()) + 2 + int(not force) + 2 * int(force)
    A = lil_array((rows, nv), dtype=np.float64)
    lo, hi, r = [], [], 0

    A[r, :n] = bytes_
    A[r, n : n + m] = fixed
    lo += [-np.inf]
    hi += [payload_cap]
    r += 1
    for i, jv in enumerate(item_job):
        A[r, i] = 1
        A[r, n + jv] = -1
        lo += [-np.inf]
        hi += [0]
        r += 1
    for jv in range(m):
        idx = np.where(item_job == jv)[0]
        A[r, idx] = -1
        A[r, n + jv] = 1
        lo += [-np.inf]
        hi += [0]
        r += 1
    for i in np.where(hot > 0)[0]:
        A[r, i] = 1
        A[r, ih] = -1
        lo += [-np.inf]
        hi += [0]
        r += 1
    A[r, :n] = -hot
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
        active = [items[i] for i, v in enumerate(x[:n]) if v]
        hotc = int(hot @ x[:n])
        return {"x": x, "active": active, "used": used, "hotc": hotc}

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
        return b["active"] if b else a["active"]

    c1 = np.zeros(nv)
    c1[:n] = bytes_
    c1[n : n + m] = fixed
    c1[ih] = diff
    a = run(c1)
    if not a:
        return []
    s = a["used"] - close_threshold(a["active"], fill, spill)
    v1 = np.zeros(nv)
    v1[:n] = bytes_
    v1[n : n + m] = fixed
    v1[ih] = diff
    c2 = np.zeros(nv)
    c2[:n] = -bytes_
    c2[n : n + m] = -fixed
    b = run(c2, [(v1, s, s)])
    if not b:
        return a["active"]
    u = b["used"]
    v2 = np.zeros(nv)
    v2[:n] = bytes_
    v2[n : n + m] = fixed
    c3 = np.zeros(nv)
    c3[:n] = -hot
    c = run(c3, [(v1, s, s), (v2, u - META_PAD, u - META_PAD)])
    return c["active"] if c else b["active"]


def pick(items: list[dict], jobs: dict, cap: int, fill: int, spill: int, force=False):
    return _solve(items, jobs, cap, fill, spill, force) if items else []


def assign_paths(pieces: list[dict]):
    files = sorted({(p["job"], p["file"], p["rel"]) for p in pieces}, key=lambda x: (x[0], x[2], str(x[1])))
    base = {(j, fid): i for i, (j, fid, _) in enumerate(files)}
    out = {}
    for p in pieces:
        k, w = base[(p["job"], p["file"])], max(3, len(str(p["n"])))
        f = f"{STORE}/{k}" + ("" if p["n"] == 1 else f".{p['i'] + 1:0{w}d}")
        out[(p["job"], p["file"], p["i"])] = (f, f + ".meta.yaml")
    return out


def manifest_bytes(part: str, cfg: dict, jobs: dict, items: list[dict], fmap: dict):
    pieces_by_file: dict[tuple[str, int], list[dict]] = {}
    for item in items:
        for piece in item["pieces"]:
            pieces_by_file.setdefault((piece["job"], piece["file"]), []).append(piece)

    manifest_jobs: list[dict] = []
    for job in sorted({x["job"] for x in items}):
        job_files = []
        for file_meta in sorted(jobs[job]["files"], key=lambda x: x["rel"]):
            present = sorted(
                pieces_by_file.get((job, file_meta["id"]), []),
                key=lambda x: x["i"],
            )
            if file_meta["piece_count"] > 1:
                archive = {
                    "count": file_meta["piece_count"],
                    "chunks": [fmap[(job, file_meta["id"], piece["i"])][0] for piece in present],
                }
                job_files.append(manifest_file_entry(file_meta["rel"], file_meta["sha256"], archive))
            elif present:
                job_files.append(
                    manifest_file_entry(
                        file_meta["rel"],
                        file_meta["sha256"],
                        fmap[(job, file_meta["id"], 0)][0],
                    )
                )
            else:
                job_files.append(manifest_file_entry(file_meta["rel"], file_meta["sha256"]))
        manifest_jobs.append({"name": job, "files": job_files})

    return manifest_dump(part, manifest_jobs)


def recovery_readme_bytes(part: str) -> bytes:
    lines = [
        f"Archive disc: {part}",
        "",
        "This README.txt is intentionally plaintext. Every other leaf file on this disc is age-encrypted.",
        "",
        "Requirements:",
        "- age CLI with age-plugin-batchpass in PATH",
        "- the archive passphrase used when this disc was created",
        "",
        "Set the passphrase in your shell:",
        "  export AGE_PASSPHRASE='your-passphrase'",
        "",
        "Decrypt the manifest for this disc:",
        "  age -d -j batchpass MANIFEST.yml > MANIFEST.dec.yml",
        "",
        "The decrypted manifest is manifest/v1 YAML:",
        "- schema: manifest/v1",
        "- jobs[*].files[*].archive is omitted when an unsplit file has no payload on this disc",
        "- archive is a string for an unsplit payload on this disc",
        "- archive.count and archive.chunks describe split payloads present on this disc",
        "",
        "Decrypt a payload:",
        "  age -d -j batchpass files/<entry> > recovered.bin",
        "",
        "Decrypt a sidecar for any payload listed above:",
        "  age -d -j batchpass files/<entry>.meta.yaml > files/<entry>.meta.dec.yaml",
        "",
        "Per-job hash manifests and OpenTimestamps proofs are stored under jobs/<job>/ on any disc carrying that job.",
        "",
        "For split files, gather every chunk from every required disc and concatenate them in chunk-index order.",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def build_disc(state_dir: Path, s: dict, items: list[dict], out_dir: Path, emit=True, part: str | None = None):
    part = part or ts_name(s.get("last_closed", ""))
    pieces = [p for it in items for p in it["pieces"]]
    jobs_on_disc = sorted({x["job"] for x in items})
    fmap = assign_paths(pieces)
    man = manifest_bytes(part, s["cfg"], s["jobs"], items, fmap)
    readme = recovery_readme_bytes(part)
    evidence_payload = sum(entry["encrypted_size"] for job in jobs_on_disc for entry in s["jobs"][job]["artifacts"])
    payload = 0
    for it in items:
        meta = {f["id"]: f for f in s["jobs"][it["job"]]["files"]}
        for p in it["pieces"]:
            payload += (state_dir / p["store"]).stat().st_size
            payload += encrypted_size_for_plaintext_size(len(sidecar_bytes(meta[p["file"]], p["i"], p["n"])))
    used = encrypted_size_for_plaintext_size(len(man)) + len(readme) + payload + evidence_payload
    if used > s["cfg"]["target"]:
        return None
    root = out_dir / part
    out = {"name": part, "path": str(root.resolve()), "used": used, "free": s["cfg"]["target"] - used, "jobs": jobs_on_disc, "items": [x["id"] for x in items], "pieces": []}
    if not emit:
        return out
    (root / STORE).mkdir(parents=True, exist_ok=True)
    encrypt_bytes_to_file(man, root / MANIFEST)
    (root / README).write_bytes(readme)
    for job_id in jobs_on_disc:
        for artifact in s["jobs"][job_id]["artifacts"]:
            encrypt_bytes_to_file(Path(artifact["source"]).read_bytes(), root / artifact["disc_relpath"])
    for it in items:
        meta = {f["id"]: f for f in s["jobs"][it["job"]]["files"]}
        for p in it["pieces"]:
            src = state_dir / p["store"]
            f, m = fmap[(p["job"], p["file"], p["i"])]
            copy_span(str(src), str(root / f))
            encrypt_bytes_to_file(sidecar_bytes(meta[p["file"]], p["i"], p["n"]), root / m)
            out["pieces"].append({"job": p["job"], "job_file_id": p["file"], "relative_path": p["rel"], "payload_relpath": f, "sidecar_relpath": m, "payload_size_bytes": p["data"], "chunk_index": None if p["n"] == 1 else p["i"] + 1, "chunk_count": None if p["n"] == 1 else p["n"]})
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
    live = {x["job"] for x in s["items"]}
    for job in list(s["jobs"]):
        if job not in live:
            shutil.rmtree(state_dir / "pool" / job, ignore_errors=True)
            del s["jobs"][job]


def flush(state_dir: Path, s: dict, out_dir: Path, force=False):
    out = []
    while True:
        cand = pick(s["items"], s["jobs"], s["cfg"]["target"], s["cfg"]["fill"], s["cfg"]["spill_fill"], force)
        forced = force
        if not cand and sum(x["bytes"] for x in s["items"]) > s["cfg"]["buffer_max"] and s["items"]:
            cand, forced = (pick(s["items"], s["jobs"], s["cfg"]["target"], s["cfg"]["fill"], s["cfg"]["spill_fill"], True), True)
        if not cand:
            break
        part = ts_name(s.get("last_closed", ""))
        while cand:
            made = build_disc(state_dir, s, cand, out_dir, False, part)
            if made:
                break
            cand = cand[:-1]
        if not cand:
            break
        req = close_threshold(cand, s["cfg"]["fill"], s["cfg"]["spill_fill"])
        if not forced and made["used"] < req:
            break
        made = build_disc(state_dir, s, cand, out_dir, True, part)
        out.append(made)
        gc_state(state_dir, s, cand)
        if not force:
            continue
    return out


def build_job_structures(job: Job):
    files = []
    kids: dict[str, list] = {"": []}
    dirs = {""}
    explicit_dirs = {d.relative_path for d in job.directories}
    for d in explicit_dirs:
        parts = d.split("/")
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    for jf in sorted(job.files, key=lambda x: x.relative_path):
        if not jf.buffer_abs_path or not Path(jf.buffer_abs_path).exists():
            raise RuntimeError(f"job file {jf.relative_path} is not present in hot buffer")
        rel = jf.relative_path
        for parent in [""] + ["/".join(rel.split("/")[:i]) for i in range(1, len(rel.split("/")) )]:
            dirs.add(parent)
        files.append({"id": jf.id, "job": job.id, "src": jf.buffer_abs_path, "rel": rel, "raw": jf.size_bytes, "mode": jf.mode, "mtime": jf.mtime, "uid": jf.uid, "gid": jf.gid, "sha256": jf.actual_sha256 or jf.expected_sha256})
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


def ingest_job(session: Session, job_id: str):
    job = session.execute(select(Job).where(Job.id == job_id).options(selectinload(Job.directories), selectinload(Job.files))).scalar_one()
    if job.status == "sealed":
        raise RuntimeError(f"job {job_id} already sealed")
    files, kids, dirs = build_job_structures(job)
    state_dir = PARTITIONER_STATE_DIR
    out_dir = PARTITION_ROOTS_DIR
    s = load_state(state_dir, PARTITION_CFG)
    if s["cfg"] != PARTITION_CFG:
        raise RuntimeError("partitioner state config mismatch")
    if job_id in s["jobs"] or any(x["job"] == job_id for x in s["items"]):
        raise RuntimeError(f"duplicate job {job_id}")
    if not files:
        job.status = "sealed"
        job.sealed_at = datetime.now(timezone.utc)
        save_state(state_dir, s)
        return {"job": job_id, "closed": [], "buffer_bytes": sum(x["bytes"] for x in s["items"])}

    manifest_artifact = cold_job_hash_manifest_path(job_id)
    proof_artifact = cold_job_hash_proof_path(job_id)
    if not manifest_artifact.exists() or not proof_artifact.exists():
        raise RuntimeError(f"job hash artifacts are missing for {job_id}")
    manifest_relpath, proof_relpath = job_disc_artifact_relpaths(job_id)
    artifacts = [
        {
            "source": str(manifest_artifact),
            "disc_relpath": manifest_relpath,
            "encrypted_size": encrypted_size_for_plaintext_size(manifest_artifact.stat().st_size),
        },
        {
            "source": str(proof_artifact),
            "disc_relpath": proof_relpath,
            "encrypted_size": encrypted_size_for_plaintext_size(proof_artifact.stat().st_size),
        },
    ]

    fixed = manifest_job_budget(job_id, files) + sum(item["encrypted_size"] for item in artifacts)
    stage_pieces(state_dir, job_id, files, s["cfg"]["target"], fixed)
    s["jobs"][job_id] = {
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

    def add_item(kind, hot, why, pieces, b):
        s["next_item"] += 1
        s["items"].append({"id": f"{s['next_item']:08d}", "job": job_id, "kind": kind, "hot": hot, "why": why, "pieces": pieces, "bytes": b})

    total = META_PAD + fixed + sum(p["est"] for f in files for p in f["pieces"])
    if total <= s["cfg"]["target"] and all(f["raw"] <= s["cfg"]["target"] for f in files):
        add_item("job", False, "job", [{"job": job_id, "file": p["file"], "rel": p["rel"], "store": p["store"], "data": p["data"], "i": p["i"], "n": p["n"]} for f in files for p in f["pieces"]], sum(p["est"] for f in files for p in f["pieces"]))
    else:
        for q in split_job(files, kids, dirs, s["cfg"]["target"] - META_PAD - fixed):
            add_item("rem", True, q["why"], q["pieces"], q["bytes"])
    closed = flush(state_dir, s, out_dir)
    job.status = "sealed"
    job.sealed_at = datetime.now(timezone.utc)
    save_state(state_dir, s)
    return {"job": job_id, "closed": closed, "buffer_bytes": sum(x["bytes"] for x in s["items"])}


def force_close_pending(session: Session):
    state_dir = PARTITIONER_STATE_DIR
    out_dir = PARTITION_ROOTS_DIR
    s = load_state(state_dir, PARTITION_CFG)
    closed = flush(state_dir, s, out_dir, force=True)
    save_state(state_dir, s)
    return closed


def import_closed_discs(session: Session, closed: list[dict]) -> list[str]:
    from .notifications import create_disc_finalization_notifications_for_disc

    disc_ids: list[str] = []
    for item in closed:
        disc_id = item["name"]
        root = Path(item["path"])
        contents_hash, total_bytes, rows = canonical_tree_hash(root)
        disc = session.get(Disc, disc_id)
        if disc is None:
            disc = Disc(id=disc_id, status="offline", root_abs_path=str(root), contents_hash=contents_hash, total_root_bytes=total_bytes)
            session.add(disc)
        else:
            disc.root_abs_path = str(root)
            disc.contents_hash = contents_hash
            disc.total_root_bytes = total_bytes
        session.flush()
        session.query(DiscEntry).filter(DiscEntry.disc_id == disc_id).delete()
        session.query(ArchivePiece).filter(ArchivePiece.disc_id == disc_id).delete()
        for row in rows:
            kind = "payload"
            rel = row["relative_path"]
            if rel == MANIFEST:
                kind = "manifest"
            elif rel == README:
                kind = "readme"
            elif str(rel).endswith(".meta.yaml"):
                kind = "sidecar"
            elif str(rel).startswith("jobs/") and str(rel).endswith("/HASHES.yml"):
                kind = "job_hash_manifest"
            elif str(rel).startswith("jobs/") and str(rel).endswith("/HASHES.yml.ots"):
                kind = "job_hash_proof"
            logical_sha256, logical_size = logical_file_sha256_and_size(root / rel)
            session.add(
                DiscEntry(
                    disc_id=disc_id,
                    relative_path=str(rel),
                    kind=kind,
                    size_bytes=logical_size,
                    sha256=logical_sha256,
                    stored_size_bytes=int(row["size_bytes"]),
                    stored_sha256=str(row["sha256"]),
                )
            )
        for p in item.get("pieces", []):
            session.add(ArchivePiece(disc_id=disc_id, job_file_id=p["job_file_id"], payload_relpath=p["payload_relpath"], sidecar_relpath=p["sidecar_relpath"], payload_size_bytes=p["payload_size_bytes"], chunk_index=p["chunk_index"], chunk_count=p["chunk_count"]))
        create_disc_finalization_notifications_for_disc(session, disc_id)
        disc_ids.append(disc_id)
    session.commit()
    return disc_ids
