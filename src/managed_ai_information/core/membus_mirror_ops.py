"""Chunked batching, checkpointing, and cold-archive helpers for MembusStoreMirror."""

import zlib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from managed_ai_information.core import storage as _st
from managed_ai_information.core import storage_backend_protocol as _sbp

DEFAULT_MIRROR_CHUNK_SIZE = 200
DEFAULT_MIRROR_MAX_BATCH_BYTES = _st.MAX_BYTES - 131_072
MIRROR_CHECKPOINT_FORMAT = "managed-ai-membus-mirror-checkpoint/1"
COLD_RECORD_KINDS = frozenset(
    {"cold-record", "archived-history", "historical-bundle", "cw-cycle-archive"}
)


def _chunk(
    seq: Sequence[Any], sz_fn: Callable[[Any], int], lim: int, max_b: int
) -> list[tuple[Any, ...]]:
    if type(lim) is not int or lim < 1:
        raise ValueError("chunk_size must be a positive integer")
    if type(max_b) is not int or not (1 <= max_b < _st.MAX_BYTES):
        raise ValueError(f"max_batch_bytes must be an integer in [1, {_st.MAX_BYTES - 1}]")
    out: list[tuple[Any, ...]] = []
    cur: list[Any] = []
    cb = 0
    for it in seq:
        if (sz := sz_fn(it)) > max_b:
            raise ValueError(f"mirror item exceeds max_batch_bytes ({max_b})")
        if cur and (len(cur) >= lim or cb + sz >= max_b):
            out.append(tuple(cur))
            cur, cb = [], 0
        cur.append(it)
        cb += sz
    if cur:
        out.append(tuple(cur))
    return out


def _rec_tok(it: Any) -> str:
    return f"{it.key}:{it.expected_sha256}:{it.visibility}"


def _ckpt_load(p: Path | None, ns: str) -> tuple[set[str], set[str]]:
    if p is None:
        return set(), set()
    _sbp.ensure_ordinary_leaf_or_missing(p)
    if not p.exists():
        return set(), set()
    d = _st.read_json(p)
    if d.get("format") != MIRROR_CHECKPOINT_FORMAT or d.get("namespace") != ns:
        raise ValueError("invalid or mismatched mirror checkpoint")
    return (
        {str(t) for t in d.get("completed_record_tokens", ())},
        {str(b) for b in d.get("completed_blobs", ())},
    )


def _ckpt_save(
    p: Path | None, ns: str, toks: set[str], keys: set[str], b: set[str], i: int, tot: int
) -> None:
    if p is None:
        return
    _sbp.ensure_ordinary_dir(p.parent)
    _sbp.ensure_ordinary_leaf_or_missing(p)
    _st.atomic_bytes(
        p,
        _st.encode(
            {
                "format": MIRROR_CHECKPOINT_FORMAT,
                "namespace": ns,
                "chunk_index": i,
                "total_chunks": tot,
                "completed_record_tokens": sorted(toks),
                "completed_record_keys": sorted(keys),
                "completed_blobs": sorted(b),
                "complete": i == tot,
            }
        ),
    )


def _decompress_blob(blob: Path, exp_b: int, exp_sha: str) -> bytes:
    dec, parts, tot = zlib.decompressobj(), [], 0
    try:
        with blob.open("rb") as f:
            while chunk := f.read(65536):
                out = dec.decompress(chunk, (exp_b + 1) - tot)
                parts.append(out)
                tot += len(out)
                if tot > exp_b or dec.unconsumed_tail:
                    raise ValueError(f"cold archive blob too large: {blob}")
                if dec.eof:
                    if dec.unused_data or f.read(1):
                        raise ValueError(f"trailing bytes in cold archive blob: {blob}")
                    break
        tail = dec.flush((exp_b + 1) - tot)
        parts.append(tail)
        tot += len(tail)
    except zlib.error as exc:
        raise ValueError(f"corrupt cold archive blob: {exc}") from None
    raw = b"".join(parts)
    if tot != exp_b or not dec.eof or _st.sha(raw) != exp_sha:
        raise ValueError(f"corrupt cold archive blob: {blob}")
    return raw


def read_cold_archive_payload(root: Path, rid: str, exp_b: int, exp_sha: str) -> bytes:
    if type(exp_b) is not int or not (0 <= exp_b <= _st.MAX_BYTES):
        raise ValueError(f"invalid cold archive raw_bytes for {rid}: {exp_b!r}")
    cdir = root / "exchange/cold-records"
    blob = cdir / "records" / f"{rid}.json.deflate"
    _sbp.ensure_ordinary_leaf_or_missing(blob)
    if blob.exists():
        return _decompress_blob(blob, exp_b, exp_sha)
    if _st.has_pack_pointer(packs := cdir / "packs"):
        with _st.PackSession(packs) as s:
            if (pk := s.read(exp_sha, retained=True, expected_bytes=exp_b)) is not None:
                return pk
    raise FileNotFoundError(f"cold-archived record unavailable: {rid}")


def _sealed_basis_obj(rec: Mapping[str, Any], objs: _st.ObjectStore | None) -> bool:
    if objs is None or not isinstance(basis := rec.get("basis"), list):
        return False
    return any(
        isinstance(e, Mapping)
        and isinstance(p := e.get("path"), str)
        and p.startswith("object:")
        and len(d := p.removeprefix("object:")) == 64
        and objs.occ_of(d) == _st.OCC_SEALED
        for e in basis
    )


def scan_cold_archive_records(
    root: Path, cfg: Any, classify: Any, item_cls: Any, sealed_fn: Any
) -> tuple[list[Any], list[str]]:
    mp = root / "exchange/cold-records/manifest.json"
    if not mp.exists() and not mp.is_symlink():
        return [], []
    _sbp.ensure_ordinary_leaf_or_missing(mp)
    ents = _st.strict_json_loads(mp.read_bytes(), "cold-records/manifest.json").get("records")
    if not isinstance(ents, dict):
        return [], []
    objs = _st.ObjectStore(od) if (od := root / "exchange/objects").exists() else None
    items, over, rdir = [], [], root / "exchange/cold-records/records"
    for k, e in sorted(ents.items()):
        if not isinstance(e, dict):
            continue
        rid, act = _sbp.validate_identifier(str(k), "record_id"), bool(e.get("active", False))
        if not cfg.include_inactive and not act:
            continue
        key, kind = f"records/{rid}", str(e.get("kind", "cold-record"))
        rb, rsha = e.get("raw_bytes", 0), str(e.get("raw_sha256", ""))
        if type(rb) is not int or rb < 0:
            raise ValueError(f"invalid cold archive raw_bytes for {rid}: {rb!r}")
        blob = rdir / f"{rid}.json.deflate"
        vis, rsn = classify(rid, {"kind": kind, "active": act}, cfg)
        if vis == "Sealed":
            items.append(sealed_fn(rid, key, blob, kind, rsn, active=act))
        elif rb > _st.MAX_BYTES or not rsha:
            over.append(rid)
        else:
            rec = _st.strict_json_loads(
                read_cold_archive_payload(root, rid, rb, rsha), f"{rid}.json"
            )
            act, kind = bool(rec.get("active", act)), str(rec.get("kind", kind))
            if not cfg.include_inactive and not act:
                continue
            vis, rsn = classify(rid, rec, cfg)
            if vis == "Sealed" or _sealed_basis_obj(rec, objs):
                items.append(
                    sealed_fn(rid, key, blob, kind, rsn or "basis-object-sealed", active=act)
                )
            else:
                items.append(
                    item_cls(rid, key, blob, kind, rid, "MetadataOnly", None, rsha, rb, act, "cold")
                )
    return items, over


def execute_chunked_mirror(
    root: Path,
    plan: Any,
    rtr: Any,
    backend: Any,
    payload_fn: Any,
    meta_fn: Any,
    *,
    chunk_size: int = DEFAULT_MIRROR_CHUNK_SIZE,
    max_batch_bytes: int = DEFAULT_MIRROR_MAX_BATCH_BYTES,
    checkpoint_path: Path | None = None,
    on_chunk: Any = None,
) -> tuple[int, int, int, int, int, int]:
    ns = plan.namespace
    dtoks, db = _ckpt_load(checkpoint_path, ns)
    dkeys = {t.split(":", 1)[0] for t in dtoks}
    pr = [i for i in plan.records if _rec_tok(i) not in dtoks]
    pb = [p for p in plan.blobs if p[0] not in db]
    rc = _chunk(pr, lambda i: int(i.payload_bytes), chunk_size, max_batch_bytes)
    bc = _chunk(pb, lambda p: int(p[1]), chunk_size, max_batch_bytes)
    tot, rw, bw, idx = len(rc) + len(bc), 0, 0, 0
    rr, bs = len(plan.records) - len(pr), len(plan.blobs) - len(pb)

    def _emit(ph: str, n: int, cb: int, kw: str, w: int) -> None:
        _ckpt_save(checkpoint_path, ns, dtoks, dkeys, db, idx, tot)
        if on_chunk is not None:
            on_chunk(
                {
                    "phase": ph,
                    "chunk_index": idx,
                    "total_chunks": tot,
                    "items_in_chunk": n,
                    "chunk_bytes": cb,
                    kw: w,
                }
            )

    for ch in rc:
        idx, cb = idx + 1, 0
        for it in ch:
            pl = payload_fn(root, it)
            rtr.put_record(
                ns,
                it.key,
                pl,
                expected_version=None,
                visibility=it.visibility,
                metadata=meta_fn(it),
            )
            dtoks.add(_rec_tok(it))
            dkeys.add(it.key)
            rw, cb = rw + 1, cb + len(pl)
        _emit("records", len(ch), cb, "records_written", rw)
    if bc and (odir := root / "exchange/objects").exists():
        objs = _st.ObjectStore(odir)
        with objs.pack_session():
            for ch in bc:
                idx, cb = idx + 1, 0
                for d, _ in ch:
                    if backend.has_blob(ns, d):
                        bs += 1
                        db.add(d)
                    elif len(raw := objs.read(d, retained=False)) <= _st.MAX_BYTES:
                        rtr.put_blob(ns, raw)
                        db.add(d)
                        bw, cb = bw + 1, cb + len(raw)
                _emit("blobs", len(ch), cb, "blobs_written", bw)
    return rw, bw, bs, len(rc), len(bc), rr
