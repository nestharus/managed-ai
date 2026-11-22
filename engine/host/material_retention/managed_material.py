#!/usr/bin/env python3
"""Content-addressed material custody, exact ZIP recipes and selected exports.

Python standard library only. This is a maintenance layer over existing byte
identities, not a replacement host/native runtime or semantic acceptance tool.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import fnmatch
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import struct
import sys
import time
import zipfile
import zlib

FORMAT = "managed-material/1"
MAX_ZIP_DEPTH = 12
MAX_ZIP_MEMBERS = 200000
MAX_MEMBER_BYTES = 1024 ** 3
MAX_EXPANDED_BYTES = 4 * 1024 ** 3
LOADED_PRODUCER_BYTES = Path(__file__).read_bytes()
RAW_BODY_KINDS = {"blob", "slice-view", "record-view", "recipe-view"}
IMMUTABLE_RECORD_DIRS = ("manifests", "selections", "lifecycle", "occurrences", "transitions")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def js(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def immutable(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(path.read_bytes() == raw, "Immutable object conflict: " + str(path))
        return
    with path.open("xb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())


def replace_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".pending")
    with temp.open("wb") as f:
        f.write(js(value))
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def deflate(raw, level=6, mem_level=8, strategy=zlib.Z_DEFAULT_STRATEGY):
    c = zlib.compressobj(level, zlib.DEFLATED, -15, mem_level, strategy)
    return c.compress(raw) + c.flush()


def safe_relative(value):
    p = PurePosixPath(value)
    require(not p.is_absolute() and p.parts and ".." not in p.parts and "\\" not in value,
            "Unsafe materialization path: " + value)
    return p


class Store:
    def __init__(self, root, create=False):
        self.root = Path(root)
        index = self.root / "index.json"
        if index.exists():
            record = json.loads(index.read_bytes())
            require(record["format"] == FORMAT, "Unknown store format")
            self.objects = record["objects"]
        else:
            require(create, "Store index does not exist")
            self.root.mkdir(parents=True, exist_ok=True)
            self.objects = {}
        self.cache = collections.OrderedDict()
        self.cache_bytes = 0
        self.encoded_sources = {row["encoded_sha256"]: key for key, row in self.objects.items()
                                if row["kind"] in RAW_BODY_KINDS and "encoded_sha256" in row}
        self.recipe_sources = {row["recipe_sha256"]: key for key, row in self.objects.items() if row["kind"] == "zip-recipe"}
        self.encoded_recipe_sources = {row["recipe_encoded_sha256"]: key for key, row in self.objects.items()
                                       if row["kind"] == "zip-recipe" and row.get("recipe_encoding") == "raw-deflate"}
        self.record_sources = {}
        for directory in IMMUTABLE_RECORD_DIRS:
            for path in (self.root / directory).rglob("*.json"):
                require(not path.is_symlink(), "Immutable record must not be a symlink")
                raw = path.read_bytes()
                ident = sha(raw)
                if directory != "occurrences":
                    require(path.stem == ident, "Immutable record name differs: " + str(path))
                self.record_sources.setdefault(ident, path.relative_to(self.root).as_posix())
        self.building = set()
        self.zip_expanded = 0
        self.observations = []

    def event(self, kind, data):
        path = self.root / "events.jsonl"
        with path.open("ab") as f:
            f.write(js({"at": utc(), "kind": kind, **data}))
            f.flush()
            os.fsync(f.fileno())

    def commit_index(self):
        replace_json(self.root / "index.json", {"format": FORMAT, "objects": self.objects})

    def cache_put(self, key, raw):
        if len(raw) > 64 * 1024 ** 2:
            return
        if key in self.cache:
            self.cache.move_to_end(key)
            return
        self.cache[key] = raw
        self.cache_bytes += len(raw)
        while self.cache_bytes > 96 * 1024 ** 2:
            _, old = self.cache.popitem(last=False)
            self.cache_bytes -= len(old)

    def blob(self, raw):
        key = sha(raw)
        if key in self.objects:
            require(self.materialize(key) == raw, "Existing body differs: " + key)
            return key
        recipe_source = self.recipe_sources.get(key)
        if recipe_source is not None and recipe_source not in self.building:
            require(self.recipe_raw(recipe_source) == raw, "Immutable recipe body differs")
            encoded = deflate(raw)
            self.objects[key] = {"kind": "recipe-view", "bytes": len(raw), "source": recipe_source,
                                 "dependencies": [recipe_source], "encoding": "raw-deflate",
                                 "encoded_sha256": sha(encoded), "encoded_bytes": len(encoded)}
            self.encoded_sources[sha(encoded)] = key
            self.cache_put(key, raw)
            return key
        record_path = self.record_sources.get(key)
        if record_path is None:
            for directory in ("manifests", "selections", "lifecycle", "transitions"):
                path = self.root / directory / (key + ".json")
                if path.is_file():
                    require(path.read_bytes() == raw, "Immutable metadata differs")
                    record_path = path.relative_to(self.root).as_posix()
                    self.record_sources[key] = record_path
                    break
        if record_path is not None:
            require((self.root / record_path).read_bytes() == raw, "Immutable record body differs")
            encoded = deflate(raw)
            self.objects[key] = {"kind": "record-view", "bytes": len(raw), "record_path": record_path,
                                 "record_sha256": key, "encoding": "raw-deflate",
                                 "encoded_sha256": sha(encoded), "encoded_bytes": len(encoded)}
            self.encoded_sources[sha(encoded)] = key
            self.cache_put(key, raw)
            return key
        if key in self.encoded_sources:
            source = self.encoded_sources[key]
            require(self.encoded_blob(source) == raw, "Persisted encoded body differs")
            self.objects[key] = {"kind": "encoded-view", "bytes": len(raw), "source": source,
                                 "dependencies": [source]}
            self.cache_put(key, raw)
            return key
        # Compressed recipe members in a later managed export are already
        # persisted metadata encodings. Recognize verified bytes independently
        # of their transport filename, using the existing recipe-view contract.
        recipe_source = self.encoded_recipe_sources.get(key)
        if recipe_source is not None and recipe_source not in self.building:
            logical = self.recipe_raw(recipe_source)
            require((self.root / self.objects[recipe_source]["path"]).read_bytes() == raw,
                    "Persisted recipe encoding differs")
            source = self.blob(logical)
            entry = self.objects[source]
            if entry["kind"] in RAW_BODY_KINDS and entry.get("encoded_sha256") == key:
                require(source != key and self.encoded_blob(source) == raw, "Recipe encoding view differs")
                self.objects[key] = {"kind": "encoded-view", "bytes": len(raw), "source": source,
                                     "dependencies": [source]}
                self.cache_put(key, raw)
                return key
        if key not in self.objects:
            encoded = deflate(raw)
            relative = "objects/" + key[:2] + "/" + key[2:] + ".deflate"
            immutable(self.root / relative, encoded)
            self.objects[key] = {"kind": "blob", "bytes": len(raw), "path": relative,
                                 "encoding": "raw-deflate", "encoded_sha256": sha(encoded),
                                 "encoded_bytes": len(encoded)}
            self.encoded_sources[sha(encoded)] = key
        self.cache_put(key, raw)
        return key

    def encoded_blob(self, key, stack=()):
        info = self.objects[key]
        require(info["kind"] in RAW_BODY_KINDS, "No body encoding for a virtual transport")
        if info["kind"] == "blob":
            encoded = (self.root / info["path"]).read_bytes()
        elif info["kind"] == "recipe-view" and self.objects[info["source"]].get("recipe_encoding") == "raw-deflate":
            encoded = (self.root / self.objects[info["source"]]["path"]).read_bytes()
        else:
            encoded = deflate(self.materialize(key, stack))
        require(len(encoded) == info["encoded_bytes"] and sha(encoded) == info["encoded_sha256"],
                "Encoded body differs: " + key)
        return encoded

    def slice_view(self, raw, source, start, end):
        key = sha(raw)
        if key in self.objects:
            require(self.materialize(key) == raw, "Existing framing identity differs")
            return key
        require(key != source and len(raw) == end - start, "Invalid framing slice")
        encoded = deflate(raw)
        self.objects[key] = {"kind": "slice-view", "bytes": len(raw), "source": source,
                             "start": start, "end": end, "dependencies": [source],
                             "encoding": "raw-deflate", "encoded_sha256": sha(encoded),
                             "encoded_bytes": len(encoded)}
        self.encoded_sources[sha(encoded)] = key
        self.cache_put(key, raw)
        return key

    def ingest(self, raw, hint="", depth=0):
        key = sha(raw)
        if key in self.objects:
            require(self.materialize(key) == raw, "Existing material differs: " + key)
            return key
        # A managed export's persisted encoding is a view of its canonical raw
        # body, not another independent copy of that body. Verify the filename
        # identity before recognizing this interchange representation.
        parts = PurePosixPath(hint).parts
        if len(parts) >= 3 and parts[-3] == "objects" and parts[-1].endswith(".deflate"):
            expected = parts[-2] + parts[-1][:-8]
            if len(expected) == 64 and all(c in "0123456789abcdef" for c in expected):
                try:
                    decoded = zlib.decompress(raw, -15)
                except zlib.error:
                    decoded = None
                if decoded is not None and sha(decoded) == expected:
                    require(len(decoded) <= MAX_MEMBER_BYTES, "Managed encoded body size limit")
                    self.blob(decoded)
                    return self.blob(raw)
        maybe_zip = hint.lower().endswith(".zip") or raw.startswith((b"PK\x03\x04", b"PK\x05\x06"))
        if maybe_zip and zipfile.is_zipfile(io.BytesIO(raw)):
            return self.ingest_zip(raw, key, depth)
        if hint.lower().endswith(".zip"):
            self.observations.append({"kind": "invalid-zip-retained-as-bytes", "hint": hint, "object": key})
        return self.blob(raw)

    def ingest_zip(self, raw, key, depth):
        require(depth < MAX_ZIP_DEPTH, "ZIP depth limit; no selection published")
        require(key not in self.building, "Cyclic transport identity")
        self.building.add(key)
        parts, members, dependencies = [], [], set()
        methods = collections.Counter()
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            infos = z.infolist()
            require(len(infos) <= MAX_ZIP_MEMBERS, "ZIP member limit")
            ordered = sorted(enumerate(infos), key=lambda x: x[1].header_offset)
            # Keep historical frame identities addressable, but place the frame
            # bytes in one packed body rather than one physical file per header.
            frame_chunks, previous_end = [], 0
            for _, info in ordered:
                off = info.header_offset
                require(off >= previous_end and off + 30 <= len(raw), "Invalid framing interval")
                require(raw[off:off+4] == b"PK\x03\x04", "Local ZIP signature differs")
                name_len, extra_len = struct.unpack_from("<HH", raw, off + 26)
                start = off + 30 + name_len + extra_len
                end = start + info.compress_size
                require(previous_end <= start <= end <= len(raw), "Invalid framing extent")
                frame_chunks.append(raw[previous_end:start]); previous_end = end
            tails = [raw[:len(raw)//2], raw[len(raw)//2:]] if previous_end == 0 else [raw[previous_end:]]
            frame_chunks.extend(tails)
            domain = b"managed-material-zip-framing/1\x00"
            framing = self.blob(domain + b"".join(frame_chunks))
            frame_keys, frame_position = [], len(domain)
            for fragment in frame_chunks:
                frame_keys.append(self.slice_view(fragment, framing, frame_position, frame_position + len(fragment)))
                frame_position += len(fragment)
            frame_iter = iter(frame_keys)
            cursor = 0
            for ordinal, info in ordered:
                require(info.file_size <= MAX_MEMBER_BYTES, "ZIP member size limit")
                self.zip_expanded += info.file_size
                require(self.zip_expanded <= MAX_EXPANDED_BYTES, "Expanded ZIP byte limit")
                off = info.header_offset
                require(off >= cursor and off + 30 <= len(raw), "Overlapping or invalid local header")
                require(raw[off:off+4] == b"PK\x03\x04", "Local ZIP signature differs")
                name_len, extra_len = struct.unpack_from("<HH", raw, off + 26)
                start = off + 30 + name_len + extra_len
                end = start + info.compress_size
                require(cursor <= start <= end <= len(raw), "Invalid compressed member interval")
                frame = next(frame_iter)
                dependencies.add(frame)
                parts.append({"kind": "frame", "object": frame})
                compressed = raw[start:end]
                compressed_sha = sha(compressed)
                content = None
                problem = None
                try:
                    body = z.read(info)
                    require(len(body) == info.file_size, "ZIP decoded size differs")
                    content = self.ingest(body, info.filename, depth + 1)
                    dependencies.add(content)
                except (NotImplementedError, RuntimeError, zipfile.BadZipFile) as exc:
                    problem = type(exc).__name__ + ": " + str(exc)
                part = None
                if content is not None and info.compress_type == zipfile.ZIP_STORED and body == compressed:
                    part = {"kind": "stored", "content": content}
                elif content is not None and info.compress_type == zipfile.ZIP_DEFLATED:
                    entry = self.objects[content]
                    if entry["kind"] in RAW_BODY_KINDS and entry["encoded_sha256"] == compressed_sha:
                        part = {"kind": "blob-encoding", "content": content}
                    else:
                        for level in (6, 9, 1, 2, 3, 4, 5, 7, 8, 0):
                            if deflate(body, level=level) == compressed:
                                part = {"kind": "deflate-recipe", "content": content,
                                        "level": level, "mem_level": 8, "strategy": 0,
                                        "verified_encoder": {"zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
                                                             "zlib_compile": zlib.ZLIB_VERSION}}
                                break
                if part is None:
                    encoded_id = self.blob(compressed)
                    dependencies.add(encoded_id)
                    part = {"kind": "encoded-fallback", "object": encoded_id,
                            "reason": problem or "No exact supported compression recipe matched",
                            "content": content}
                part.update({"sha256": compressed_sha, "bytes": len(compressed)})
                methods[part["kind"]] += 1
                part_index = len(parts)
                parts.append(part)
                members.append({"ordinal": ordinal, "name": info.filename, "is_directory": info.is_dir(),
                                "bytes": info.file_size, "compressed_bytes": info.compress_size,
                                "compression": info.compress_type, "flags": info.flag_bits,
                                "crc32": info.CRC, "content": content, "part": part_index,
                                "semantic_content": "retained" if content is not None else "unavailable",
                                **({"decode_error": problem} if problem else {})})
                cursor = end
            # An empty ZIP has only framing. Split it so the framing reference
            # cannot be the transport's own raw identity and form a cycle.
            for _ in tails:
                trailer = next(frame_iter)
                dependencies.add(trailer)
                parts.append({"kind": "frame", "object": trailer})
        recipe = {"format": "managed-material-zip/1", "sha256": key, "bytes": len(raw),
                  "parts": parts, "members": sorted(members, key=lambda x: x["ordinal"]),
                  "dependencies": sorted(dependencies), "member_encodings": dict(methods)}
        recipe_raw = js(recipe)
        recipe_encoded = deflate(recipe_raw)
        relative = "recipes/" + key + ".json.deflate"
        immutable(self.root / relative, recipe_encoded)
        self.objects[key] = {"kind": "zip-recipe", "bytes": len(raw), "path": relative,
                             "recipe_sha256": sha(recipe_raw), "recipe_bytes": len(recipe_raw),
                             "recipe_encoding": "raw-deflate", "recipe_encoded_sha256": sha(recipe_encoded),
                             "recipe_encoded_bytes": len(recipe_encoded),
                             "dependencies": sorted(dependencies), "member_encodings": dict(methods),
                             "members": len(members)}
        self.building.remove(key)
        self.recipe_sources[sha(recipe_raw)] = key
        self.encoded_recipe_sources[sha(recipe_encoded)] = key
        require(self.materialize(key) == raw, "Immediate exact ZIP reconstruction differs: " + key)
        self.cache_put(key, raw)
        return key

    def recipe_raw(self, key):
        info = self.objects[key]
        raw = (self.root / info["path"]).read_bytes()
        if info.get("recipe_encoding") == "raw-deflate":
            require(len(raw) == info["recipe_encoded_bytes"] and sha(raw) == info["recipe_encoded_sha256"],
                    "Encoded ZIP recipe differs: " + key)
            raw = zlib.decompress(raw, -15)
        require(sha(raw) == info["recipe_sha256"], "ZIP recipe differs: " + key)
        return raw

    def recipe(self, key):
        return json.loads(self.recipe_raw(key))

    def materialize(self, key, stack=()):
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        require(key not in stack, "Cyclic content recipe")
        info = self.objects[key]
        if info["kind"] == "blob":
            raw = zlib.decompress(self.encoded_blob(key), -15)
        elif info["kind"] == "slice-view":
            source = self.materialize(info["source"], (*stack, key))
            require(0 <= info["start"] <= info["end"] <= len(source), "Framing slice is outside its body")
            raw = source[info["start"]:info["end"]]
        elif info["kind"] == "record-view":
            relative = safe_relative(info["record_path"])
            require(relative.parts[0] in IMMUTABLE_RECORD_DIRS, "Metadata view must name an immutable record")
            raw = (self.root / info["record_path"]).read_bytes()
            require(sha(raw) == info["record_sha256"], "Retained immutable record differs")
        elif info["kind"] == "recipe-view":
            require(info["source"] not in stack and info["source"] != key, "Cyclic recipe view")
            raw = self.recipe_raw(info["source"])
        elif info["kind"] == "encoded-view":
            raw = self.encoded_blob(info["source"], (*stack, key))
        elif info["kind"] == "zip-recipe":
            buf = io.BytesIO()
            for part in self.recipe(key)["parts"]:
                kind = part["kind"]
                if kind in ("frame", "encoded-fallback"):
                    fragment = self.materialize(part["object"], (*stack, key))
                elif kind == "blob-encoding":
                    fragment = self.encoded_blob(part["content"], (*stack, key))
                elif kind == "stored":
                    fragment = self.materialize(part["content"], (*stack, key))
                elif kind == "deflate-recipe":
                    fragment = deflate(self.materialize(part["content"], (*stack, key)),
                                       part["level"], part["mem_level"], part["strategy"])
                else:
                    raise ValueError("Unknown transport part kind: " + kind)
                if kind != "frame":
                    require(len(fragment) == part["bytes"] and sha(fragment) == part["sha256"],
                            "Exact compressed replay differs; original encoder may be required: " + key)
                buf.write(fragment)
            raw = buf.getvalue()
        else:
            raise ValueError("Unknown object kind")
        require(len(raw) == info["bytes"] and sha(raw) == key, "Recovered content differs: " + key)
        self.cache_put(key, raw)
        return raw

    def closure(self, roots):
        todo, found, active = [(key, False) for key in roots], set(), set()
        while todo:
            key, leaving = todo.pop()
            if leaving:
                active.remove(key); found.add(key)
                continue
            if key in found:
                continue
            require(key not in active, "Cyclic content dependency")
            require(key in self.objects, "Absent dependency: " + key)
            active.add(key); todo.append((key, True))
            todo.extend((dependency, False) for dependency in self.objects[key].get("dependencies", []))
        return found

    def stats(self, keys=None):
        keys = set(self.objects) if keys is None else set(keys)
        encodings = collections.Counter()
        kinds = collections.Counter()
        for key in keys:
            record = self.objects[key]
            kinds[record["kind"]] += 1
            encodings.update(record.get("member_encodings", {}))
        return {"object_count": len(keys), "kinds": dict(kinds), "zip_member_encodings": dict(encodings),
                "persisted_payload_bytes": sum((self.root / self.objects[x]["path"]).stat().st_size for x in keys if "path" in self.objects[x]),
                "canonical_blob_bytes": sum(self.objects[x]["bytes"] for x in keys if self.objects[x]["kind"] == "blob"),
                "virtual_zip_bytes": sum(self.objects[x]["bytes"] for x in keys if self.objects[x]["kind"] == "zip-recipe"),
                "virtual_encoded_bytes": sum(self.objects[x]["bytes"] for x in keys if self.objects[x]["kind"] == "encoded-view")}


def path_state(path):
    s = path.lstat()
    return {"bytes": s.st_size, "mtime_ns": s.st_mtime_ns, "mode": stat.S_IMODE(s.st_mode),
            "device": s.st_dev, "inode": s.st_ino, "type": stat.S_IFMT(s.st_mode)}


def snapshot(store, spec_path):
    spec_raw = Path(spec_path).read_bytes()
    spec = json.loads(spec_raw)
    require(spec.get("format") == "managed-material-selection/1", "Unknown selection spec")
    require(spec.get("id"), "Explicit occurrence identity is required")
    existing = store.root / "occurrences" / (spec["id"] + ".json")
    require(not existing.exists(), "Occurrence identity already exists; retain it and select a new identity")
    safe_relative(spec["id"])
    attempt = {"id": spec["id"], "spec_sha256": sha(spec_raw), "started_at": utc()}
    store.event("snapshot-started", attempt)
    rows, exclusions, source_checks, labels = [], [], [], set()
    started = time.monotonic()
    try:
        producer_object = store.blob(LOADED_PRODUCER_BYTES)
        prior_key, prior_selection = current_selection(store) if (store.root / "CURRENT.json").exists() else (None, None)
        # A new current-use selection does not inherit every earlier delivery.
        # An explicitly supplied legacy history list remains an intentional
        # selection. Append-only ancestry is available only by an explicit policy.
        policy = spec.get("retention_policy")
        require(policy in (None, "current-use", "append-history"),
                "retention_policy must be current-use or append-history")
        history = spec.get("history", [])
        require(isinstance(history, list), "History must name exact prior manifest hashes")
        require(not (policy == "current-use" and history),
                "current-use conflicts with nonempty history; omit the policy to select exact history")
        history = list(history)
        if policy == "append-history" and prior_selection is not None:
            if prior_selection["manifest"] not in history:
                history.insert(0, prior_selection["manifest"])
        effective_policy = policy or ("selected-history" if history else "current-use")
        history_account = spec.get("history_account")
        if effective_policy == "current-use" and not history_account:
            history_account = ("Current-use selection retains the declared roots without implicit "
                               "ancestry. Earlier managed objects are not deleted.")
        retained_manifests = set()
        for old in history:
            retained_manifests.update(manifest_closure(store, old)[0])
        retain_prior_selection = prior_selection is not None and prior_selection["manifest"] in retained_manifests
        if prior_selection is not None and not retain_prior_selection:
            require(history_account, "Omitting the prior selected-state ancestry needs an explicit history account")
        prior_states = materialization_state(store, selection_closure(store, prior_key)[0]) if retain_prior_selection else []
        for root in spec["roots"]:
            label = root["label"]
            safe_relative(label)
            require(label not in labels and "/" not in label, "Unique single-component root labels required")
            labels.add(label)
            base = Path(root["path"]).absolute()
            require(base.exists(), "Selected root absent: " + str(base))
            require(store.root.resolve() != base.resolve() and base.resolve() not in store.root.resolve().parents,
                    "Store must not be inside a scanned source root")
            paths = [base]
            if base.is_dir() and not base.is_symlink():
                for parent, dirs, files in os.walk(base, followlinks=False):
                    dirs.sort(); files.sort()
                    paths.extend(Path(parent) / x for x in dirs + files)
            for path in paths:
                rel = str(path.relative_to(base)).replace(os.sep, "/") if path != base else ""
                logical = label + ("/" + rel if rel else "")
                if any(fnmatch.fnmatchcase(logical, pattern) for pattern in spec.get("exclude", [])):
                    exclusions.append({"path": logical, "reason": "explicit selection exclusion"})
                    continue
                before = path_state(path)
                row = {"path": logical, "source_path": str(path), "mode": before["mode"],
                       "mtime_ns": before["mtime_ns"], "role": root.get("role", "observed-material"),
                       "materialization": root.get("materialization", "observed-working-copy")}
                if path.is_symlink():
                    raw = os.fsencode(os.readlink(path))
                    key = store.blob(raw)
                    row.update({"type": "symlink", "object": key, "bytes": len(raw)})
                elif path.is_file():
                    raw = path.read_bytes()
                    after = path_state(path)
                    require(before == after, "Source changed during acquisition: " + str(path))
                    key = store.ingest(raw, logical)
                    row.update({"type": "file", "object": key, "bytes": len(raw)})
                    source_checks.append({"path": str(path), "state": after, "sha256": key})
                elif path.is_dir():
                    row.update({"type": "directory"})
                else:
                    raise ValueError("Special filesystem object requires explicit handling: " + str(path))
                rows.append(row)
        logicals = [r["path"] for r in rows]
        require(len(logicals) == len(set(logicals)), "Duplicate occurrence path")
        by_path = {r["path"]: r for r in rows}
        currents = []
        for current in spec.get("current", []):
            selected = by_path.get(current["path"])
            require(selected is not None and "object" in selected, "Current choice absent from observed occurrence: " + current["path"])
            if current.get("expected_sha256"):
                require(selected["object"] == current["expected_sha256"], "Current selection identity differs")
            currents.append({**current, "object": selected["object"], "bytes": selected["bytes"]})
        for check in source_checks:
            path = Path(check["path"])
            require(path_state(path) == check["state"] and sha(path.read_bytes()) == check["sha256"],
                    "Source changed before selection publication: " + str(path))
        manifest = {"format": "managed-material-occurrence/1", "id": spec["id"],
                    "started_at": attempt["started_at"], "completed_at": utc(),
                    "spec": spec, "spec_sha256": sha(spec_raw), "files": rows, "excluded": exclusions,
                    "current": currents, "lineage": spec.get("lineage", []),
                    "history": history,
                    "retention": {"policy": effective_policy, "history_account": history_account},
                    "materialization_groups": spec.get("materialization_groups", []),
                    "producer": {"sha256": producer_object, "object": producer_object, "python": platform.python_version(),
                                 "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION, "zlib_compile": zlib.ZLIB_VERSION},
                    "observations": store.observations,
                    "scope": "Per-file stable custody plus final identity recheck; not an atomic source transaction, semantic acceptance, runtime relocation, or deletion authority."}
        raw = js(manifest)
        manifest_hash = sha(raw)
        store.commit_index()
        immutable(store.root / "manifests" / (manifest_hash + ".json"), raw)
        pointer = {"format": "managed-material-occurrence-pointer/1", "id": spec["id"], "manifest": manifest_hash,
                   "path": "manifests/" + manifest_hash + ".json"}
        immutable(existing, js(pointer))
        selection = {"format": "managed-material-current-selection/1", "occurrence": spec["id"],
                     "manifest": manifest_hash, "current": currents,
                     "account": spec.get("account", "Observed selection; no implied acceptance.")}
        if retain_prior_selection:
            selection["previous_selection"] = prior_key
        selection["materializations"] = observed_materialization_state(prior_states, manifest, manifest_hash)
        selection_raw = js(selection)
        selection_hash = sha(selection_raw)
        immutable(store.root / "selections" / (selection_hash + ".json"), selection_raw)
        replace_json(store.root / "CURRENT.json", {"selection": selection_hash,
                                                   "path": "selections/" + selection_hash + ".json"})
        closure = store.closure(r["object"] for r in rows if "object" in r)
        result = {"id": spec["id"], "manifest": manifest_hash, "selection": selection_hash,
                  "retention_policy": effective_policy,
                  "occurrence_entries": len(rows), "file_occurrences": sum(r["type"] == "file" for r in rows),
                  "source_bytes": sum(r.get("bytes", 0) for r in rows), "seconds": round(time.monotonic() - started, 3),
                  **store.stats(closure)}
        store.event("snapshot-completed", result)
        return result
    except Exception as exc:
        store.commit_index()
        store.event("snapshot-failed", {**attempt, "error_type": type(exc).__name__, "error": str(exc),
                                        "completed_entries": len(rows), "selection_published": False})
        raise


def read_manifest(store, occurrence=None):
    if occurrence:
        safe_relative(occurrence)
        p = json.loads((store.root / "occurrences" / (occurrence + ".json")).read_bytes())
        key = p["manifest"]
    else:
        c = json.loads((store.root / "CURRENT.json").read_bytes())
        sraw = (store.root / c["path"]).read_bytes()
        require(sha(sraw) == c["selection"], "Current selection differs")
        key = json.loads(sraw)["manifest"]
    raw = (store.root / "manifests" / (key + ".json")).read_bytes()
    require(sha(raw) == key, "Occurrence manifest differs")
    return key, json.loads(raw), raw


def manifest_closure(store, key):
    todo, found, material = [key], {}, set()
    while todo:
        ident = todo.pop()
        if ident in found:
            continue
        require(len(ident) == 64 and all(c in "0123456789abcdef" for c in ident), "Invalid manifest identity")
        raw = (store.root / "manifests" / (ident + ".json")).read_bytes()
        require(sha(raw) == ident, "Historical manifest differs")
        manifest = json.loads(raw)
        require(manifest["format"] == "managed-material-occurrence/1", "Unknown historical manifest format")
        found[ident] = (manifest, raw)
        material.update(r["object"] for r in manifest["files"] if "object" in r)
        if manifest.get("producer", {}).get("object"):
            material.add(manifest["producer"]["object"])
        todo.extend(manifest.get("history", []))
    return found, store.closure(material)


def current_selection(store):
    pointer = json.loads((store.root / "CURRENT.json").read_bytes())
    key = pointer["selection"]
    require(pointer["path"] == "selections/" + key + ".json", "Current selection path differs")
    raw = (store.root / pointer["path"]).read_bytes()
    require(sha(raw) == key, "Current selection differs")
    selected = json.loads(raw)
    require(selected["format"] == "managed-material-current-selection/1", "Unknown selection format")
    return key, selected


def selection_closure(store, key, root=None):
    """Retained selected state includes its causal receipts and their manifests."""
    pending, selections, receipts, manifests, objects = [key], {}, {}, {}, set()
    while pending:
        ident = pending.pop()
        if ident in selections:
            continue
        require(len(ident) == 64 and all(c in "0123456789abcdef" for c in ident), "Invalid selection identity")
        raw = js(root) if ident == key and root is not None else (store.root / "selections" / (ident + ".json")).read_bytes()
        require(sha(raw) == ident, "Retained selection differs")
        selected = json.loads(raw)
        require(selected["format"] == "managed-material-current-selection/1", "Unknown retained selection format")
        selections[ident] = (selected, raw)
        selected_manifests, selected_objects = manifest_closure(store, selected["manifest"])
        manifests.update(selected_manifests); objects.update(selected_objects)
        for additional in selected.get("additional_manifests", []):
            more_manifests, more_objects = manifest_closure(store, additional)
            manifests.update(more_manifests); objects.update(more_objects)
        if selected.get("previous_selection"):
            pending.append(selected["previous_selection"])
        receipt_ids = set(selected.get("lifecycle", []))
        receipt_ids.update(r["receipt"] for r in selected.get("materializations", []) if r.get("receipt"))
        for receipt_id in receipt_ids:
            if receipt_id in receipts:
                continue
            require(len(receipt_id) == 64 and all(c in "0123456789abcdef" for c in receipt_id), "Invalid lifecycle identity")
            receipt_raw = (store.root / "lifecycle" / (receipt_id + ".json")).read_bytes()
            require(sha(receipt_raw) == receipt_id, "Lifecycle receipt differs")
            receipt = json.loads(receipt_raw)
            require(receipt["format"] == "managed-material-retirement/1", "Unknown lifecycle receipt format")
            receipts[receipt_id] = (receipt, receipt_raw)
            receipt_manifests, receipt_objects = manifest_closure(store, receipt["source_manifest"])
            manifests.update(receipt_manifests); objects.update(receipt_objects)
            if receipt.get("previous_selection"):
                pending.append(receipt["previous_selection"])
    _, transition_objects, _ = representation_closure(store, selections)
    objects.update(transition_objects)
    return selections, receipts, manifests, objects


def representation_closure(store, selections):
    """A transition's declared metadata files are exact recovery dependencies.

    Include them as historical records; do not make their selections current.
    Earlier declared transition records carry their own dependencies onward.
    """
    records, roots, files = {}, set(), {}
    pending = [key for selected, _ in selections.values()
               for key in selected.get("representation_transitions", [])]
    while pending:
        key = pending.pop()
        if key in records:
            continue
        require(len(key) == 64 and all(c in "0123456789abcdef" for c in key),
                "Invalid representation transition identity")
        raw = (store.root / "transitions" / (key + ".json")).read_bytes()
        require(sha(raw) == key, "Representation transition differs")
        record = json.loads(raw)
        require(record["format"] == "managed-material-representation-transition/1", "Unknown representation transition")
        records[key] = (record, raw)
        roots.update(record.get("evidence_objects", []))
        for declared in record.get("immutable_records", []):
            relative = safe_relative(declared["path"])
            require(len(relative.parts) >= 2 and relative.parts[0] in IMMUTABLE_RECORD_DIRS
                    and relative.suffix == ".json", "Invalid transition metadata dependency")
            path = store.root
            for part in relative.parts:
                path = path / part
                require(not path.is_symlink(), "Transition metadata dependency must not traverse a symlink")
            dependency = path.read_bytes()
            require(sha(dependency) == declared["sha256"] and len(dependency) == declared["bytes"],
                    "Transition metadata dependency differs: " + relative.as_posix())
            if relative.parts[0] != "occurrences":
                require(relative.stem == declared["sha256"], "Transition metadata name differs")
            files[relative.as_posix()] = dependency
            if relative.parts[0] == "transitions":
                pending.append(relative.stem)
    return records, store.closure(roots), files


def observed_materialization_state(previous, manifest, manifest_key):
    states = {(r["prefix"], r["source_path"]): dict(r) for r in previous}
    paths = {r["path"]: r for r in manifest["files"]}
    for group in manifest.get("materialization_groups", []):
        row = paths.get(group["prefix"])
        # An absent old group is not evidence that its retired copy reappeared.
        if row is None:
            continue
        states[(group["prefix"], row["source_path"])] = {**group, "source_path": row["source_path"],
                                                      "manifest": manifest_key, "basis": "captured-group-selection"}
    return sorted(states.values(), key=lambda r: (r["prefix"], r["source_path"]))


def materialization_state(store, selections):
    """Derive current disposition without converting retained history to active work."""
    states, seen_manifests, ordered, visited, visiting = [], set(), [], set(), set()
    def visit(key):
        if key in visited:
            return
        require(key not in visiting, "Cyclic selected-state ancestry")
        visiting.add(key)
        selected = selections[key][0]
        previous = selected.get("previous_selection")
        if previous:
            require(previous in selections, "Missing selected-state ancestor")
            visit(previous)
        visiting.remove(key); visited.add(key); ordered.append(selected)
    for selected_key in reversed(list(selections)):
        visit(selected_key)
    for selected in ordered:
        manifest_key = selected["manifest"]
        if manifest_key not in seen_manifests:
            raw = (store.root / "manifests" / (manifest_key + ".json")).read_bytes()
            require(sha(raw) == manifest_key, "Materialization basis differs")
            states = observed_materialization_state(states, json.loads(raw), manifest_key)
            seen_manifests.add(manifest_key)
        by_identity = {(r["prefix"], r["source_path"]): r for r in states}
        for materialization in selected.get("materializations", []):
            row = dict(materialization)
            if row.get("receipt"):
                receipt_raw = (store.root / "lifecycle" / (row["receipt"] + ".json")).read_bytes()
                require(sha(receipt_raw) == row["receipt"], "Materialization receipt differs")
                receipt = json.loads(receipt_raw)
                matches = [r for r in receipt["retired"] if r["prefix"] == row["prefix"]]
                require(len(matches) == 1, "Materialization lacks an unambiguous retirement basis")
                row["source_path"] = matches[0]["source_path"]
                row["manifest"] = receipt["source_manifest"]
                row["basis"] = "retirement-receipt"
            require(row.get("source_path"), "Materialization lacks a source identity")
            by_identity[(row["prefix"], row["source_path"])] = row
        states = list(by_identity.values())
    return sorted(states, key=lambda r: (r["prefix"], r["source_path"]))


def selected_for_manifest(store, manifest_key, manifest):
    current_key, selected = current_selection(store)
    selections = selection_closure(store, current_key)[0]
    for key, (candidate, _) in selections.items():
        if candidate["manifest"] == manifest_key:
            return key, candidate
    # A deliberately omitted ancestry may still be requested as body history.
    selected = {"format": "managed-material-current-selection/1", "occurrence": manifest["id"],
                "manifest": manifest_key, "current": manifest["current"],
                "materializations": observed_materialization_state([], manifest, manifest_key),
                "account": "Explicit historical occurrence; no retained selected-state ancestry was available in the current lineage."}
    return sha(js(selected)), selected


def verify(store, occurrence=None):
    key, manifest, _ = read_manifest(store, occurrence)
    selection_key, selected = selected_for_manifest(store, key, manifest)
    selections, receipts, manifests, keys = selection_closure(store, selection_key, selected)
    started = time.monotonic()
    store.cache.clear(); store.cache_bytes = 0
    for obj in sorted(keys):
        store.materialize(obj)
    return {"manifest": key, "objects_verified": len(keys), "historical_manifests_verified": len(manifests),
            "selections_verified": len(selections), "lifecycle_receipts_verified": len(receipts),
            "current_materializations": materialization_state(store, selections),
            "file_occurrences_covered": sum(r["type"] == "file" for m, _ in manifests.values() for r in m["files"]),
            "seconds": round(time.monotonic() - started, 3),
            "exact_recovery": "Every reachable body's original SHA-256 and byte count, including complete ZIP transports, matched.",
            "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION, **store.stats(keys)}


def export(store, output, occurrence=None):
    key, manifest, manifest_raw = read_manifest(store, occurrence)
    output = Path(output)
    require(not output.exists(), "Preserve prior export; select a new output identity")
    selection_key, selection = selected_for_manifest(store, key, manifest)
    selections, receipts, manifests, keys = selection_closure(store, selection_key, selection)
    # Export is constructed from a verified reference closure, never a folder walk.
    checked = verify(store, occurrence)
    files = {"index.json": js({"format": FORMAT, "objects": {k: store.objects[k] for k in sorted(keys)}}),
             "manifests/" + key + ".json": manifest_raw,
             "occurrences/" + manifest["id"] + ".json": js({"format": "managed-material-occurrence-pointer/1",
                 "id": manifest["id"], "manifest": key, "path": "manifests/" + key + ".json"}),
             "managed_material.py": LOADED_PRODUCER_BYTES,
             "RECOVERY.json": js({"format": "managed-material-recovery/1", "manifest": key,
                 "verify": "python managed_material.py verify --store . --occurrence " + manifest["id"],
                 "restore": "python managed_material.py restore --store . --occurrence " + manifest["id"] + " --output ../restored",
                 "codec_boundary": "Stored bodies decode with DEFLATE. Historical deflate-recipe parts require the recorded encoder or another encoder producing the same pinned compressed hash; mismatch refuses exact replay.",
                 "verified": checked})}
    files["CURRENT.json"] = js({"selection": selection_key, "path": "selections/" + selection_key + ".json"})
    for selected_key, (_, selected_raw) in selections.items():
        files["selections/" + selected_key + ".json"] = selected_raw
    for receipt_key, (_, receipt_raw) in receipts.items():
        files["lifecycle/" + receipt_key + ".json"] = receipt_raw
    transitions, _, transition_files = representation_closure(store, selections)
    files.update(transition_files)
    for transition_key, (_, transition_raw) in transitions.items():
        files["transitions/" + transition_key + ".json"] = transition_raw
    for old_key, (old_manifest, old_raw) in manifests.items():
        files["manifests/" + old_key + ".json"] = old_raw
        files["occurrences/" + old_manifest["id"] + ".json"] = js({"format": "managed-material-occurrence-pointer/1",
            "id": old_manifest["id"], "manifest": old_key, "path": "manifests/" + old_key + ".json"})
    for obj in sorted(keys):
        row = store.objects[obj]
        if "path" in row:
            files[row["path"]] = store.root / row["path"]
        if row["kind"] == "record-view":
            files[row["record_path"]] = store.root / row["record_path"]
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_entries = []
    with zipfile.ZipFile(output, "x") as z:
        for name, source in sorted(files.items()):
            raw = source.read_bytes() if isinstance(source, Path) else source
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.external_attr = (0o644 | stat.S_IFREG) << 16
            info.compress_type = zipfile.ZIP_STORED if name.endswith(".deflate") else zipfile.ZIP_DEFLATED
            z.writestr(info, raw)
            manifest_entries.append({"path": name, "sha256": sha(raw), "bytes": len(raw)})
        info = zipfile.ZipInfo("EXPORT-MANIFEST.json", (1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, js({"format": "managed-material-export/1", "source_manifest": key,
                            "entries": manifest_entries, "nested_archive_payloads": 0}))
    with zipfile.ZipFile(output) as z:
        for row in manifest_entries:
            require(sha(z.read(row["path"])) == row["sha256"], "Export member readback differs")
    result = {"output": str(output), "sha256": sha(output.read_bytes()), "bytes": output.stat().st_size,
              "source_manifest": key, "archive_entries": len(files) + 1, "nested_zip_files": 0,
              "historical_manifests": len(manifests),
              "retained_selections": len(selections), "retained_lifecycle_receipts": len(receipts),
              "source_occurrence_bytes": sum(r.get("bytes", 0) for r in manifest["files"]),
              "recovery_verified": True, **store.stats(keys)}
    immutable(output.with_suffix(output.suffix + ".receipt.json"), js(result))
    store.event("export-completed", result)
    return result


def restore(store, output, occurrence=None, prefix=None):
    key, manifest, _ = read_manifest(store, occurrence)
    output = Path(output)
    require(not output.exists(), "Restore target must not exist")
    output.mkdir(parents=True)
    count, byte_count, directories = 0, 0, []
    for row in manifest["files"]:
        if prefix and not (row["path"] == prefix or row["path"].startswith(prefix.rstrip("/") + "/")):
            continue
        rel = safe_relative(row["path"])
        target = output.joinpath(*rel.parts)
        # A captured symlink must never become an ancestor used by another row.
        for parent in target.parents:
            if parent == output:
                break
            require(not parent.is_symlink(), "Symlink ancestor during restore")
        if row["type"] == "directory":
            target.mkdir(parents=True, exist_ok=True); directories.append((target, row))
        elif row["type"] == "symlink":
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.fsdecode(store.materialize(row["object"])), target)
            count += 1
        elif row["type"] == "file":
            target.parent.mkdir(parents=True, exist_ok=True)
            raw = store.materialize(row["object"])
            immutable(target, raw)
            os.chmod(target, row["mode"])
            os.utime(target, ns=(row["mtime_ns"], row["mtime_ns"]))
            require(sha(target.read_bytes()) == row["object"], "Restored file readback differs")
            count += 1; byte_count += len(raw)
        else:
            raise ValueError("Unknown occurrence type")
    for target, row in reversed(directories):
        os.chmod(target, row["mode"])
        os.utime(target, ns=(row["mtime_ns"], row["mtime_ns"]))
    return {"manifest": key, "output": str(output), "entries_restored": count, "bytes": byte_count,
            "prefix": prefix, "readback": "All restored file bytes matched original identities; no relocation of captured paths."}


def retirement_plan(store, spec_path, occurrence=None):
    """Verify selected copies without removing them or deciding their applicability."""
    key, manifest, _ = read_manifest(store, occurrence)
    proposal = spec_path if isinstance(spec_path, dict) else json.loads(Path(spec_path).read_bytes())
    require(proposal.get("format") == "managed-material-retirement-proposal/1", "Unknown retirement proposal")
    require(proposal.get("account"), "Retirement proposal needs an applicability account")
    protected = proposal.get("protected_prefixes", [])
    require(protected, "Explicitly declare protected active paths")
    groups = manifest.get("materialization_groups", [])
    protected = protected + [g["prefix"] for g in groups if g["state"] == "active"]
    nominated = {g["prefix"] for g in groups if g["state"] == "retirement-candidate"}
    require(all(p in nominated for p in proposal["prefixes"]), "Retire only groups declared candidates in the captured selection")
    for i, prefix in enumerate(proposal["prefixes"]):
        require(not any(prefix == other or prefix.startswith(other.rstrip("/") + "/") or
                        other.startswith(prefix.rstrip("/") + "/") for other in proposal["prefixes"][i+1:]),
                "Overlapping retirement prefixes")
    rows_by_path = {r["path"]: r for r in manifest["files"]}
    recoverable = verify(store, occurrence)
    results = []
    for prefix in proposal["prefixes"]:
        safe_relative(prefix)
        require(not any(prefix == p or prefix.startswith(p.rstrip("/") + "/") or
                        p.startswith(prefix.rstrip("/") + "/") for p in protected),
                "Proposal overlaps a protected active materialization: " + prefix)
        require(prefix in rows_by_path, "Proposed root was not captured: " + prefix)
        require(not any(c["path"] == prefix or c["path"].startswith(prefix.rstrip("/") + "/") for c in manifest["current"]),
                "Proposed path supplies an explicitly current selected body")
        root_row = rows_by_path[prefix]
        captured = [r for r in manifest["files"] if r["path"] == prefix or r["path"].startswith(prefix.rstrip("/") + "/")]
        source = Path(root_row["source_path"])
        require(source.exists() or source.is_symlink(), "Proposed materialization is absent")
        observed = {source}
        if source.is_dir() and not source.is_symlink():
            for parent, dirs, files in os.walk(source, followlinks=False):
                observed.update(Path(parent) / n for n in dirs + files)
        require(observed == {Path(r["source_path"]) for r in captured},
                "Materialization inventory changed or was excluded: " + prefix)
        for row in captured:
            path = Path(row["source_path"])
            require(stat.S_IMODE(path.lstat().st_mode) == row["mode"], "Mode changed after capture: " + str(path))
            if row["type"] == "file":
                require(path.is_file() and not path.is_symlink() and sha(path.read_bytes()) == row["object"],
                        "Live file changed after capture: " + str(path))
            elif row["type"] == "symlink":
                require(path.is_symlink() and sha(os.fsencode(os.readlink(path))) == row["object"],
                        "Live symlink changed after capture: " + str(path))
            else:
                require(path.is_dir() and not path.is_symlink(), "Live directory kind changed")
        results.append({"prefix": prefix, "source_path": str(source), "entries": len(captured),
                        "bytes": sum(r.get("bytes", 0) for r in captured),
                        "restore": {"manifest": key, "occurrence": manifest["id"], "prefix": prefix},
                        "observed_at": utc(), "state": "byte-recoverable-and-currently-matching"})
    return {"format": "managed-material-retirement-plan/1", "source_manifest": key,
            "proposal": proposal, "recovery_verification": recoverable, "materializations": results,
            "state": "ready-for-owner-receiving", "removed_paths": 0,
            "boundary": "Physical recoverability and live equality only. Owner must establish no active dependency, select the semantic retirement and recheck immediately before any removal. This producer performs no deletion."}


def retire(store, plan_path, expected_plan, expected_selection):
    """Explicit operator command; never invoked by snapshot, verify or export."""
    plan_raw = Path(plan_path).read_bytes()
    require(sha(plan_raw) == expected_plan, "Retirement plan differs from the received plan")
    plan = json.loads(plan_raw)
    pointer = json.loads((store.root / "CURRENT.json").read_bytes())
    require(pointer["selection"] == expected_selection, "Current selection changed since receiving")
    current_raw = (store.root / pointer["path"]).read_bytes()
    require(sha(current_raw) == expected_selection, "Current selection bytes differ")
    current = json.loads(current_raw)
    require(current["manifest"] == plan["source_manifest"], "Retirement is against a different captured occurrence")
    require(plan["state"] == "ready-for-owner-receiving", "Plan was not physically validated")
    fresh = retirement_plan(store, plan["proposal"])
    expected = [{k: r[k] for k in ("prefix", "source_path", "entries", "bytes", "restore")} for r in plan["materializations"]]
    observed = [{k: r[k] for k in ("prefix", "source_path", "entries", "bytes", "restore")} for r in fresh["materializations"]]
    require(expected == observed, "Live materializations differ from received plan")
    store.event("retirement-started", {"plan": expected_plan, "selection": expected_selection,
                                        "materializations": expected})
    removed = []
    try:
        for row in fresh["materializations"]:
            source = Path(row["source_path"])
            require(source.resolve() != store.root.resolve() and source.resolve() not in store.root.resolve().parents,
                    "Cannot remove the managed store")
            if source.is_dir() and not source.is_symlink():
                shutil.rmtree(source)
            else:
                source.unlink()
            removed.append(row)
            store.event("materialization-retired", {"plan": expected_plan, "materialization": row})
    except Exception as exc:
        store.event("retirement-partial", {"plan": expected_plan, "removed": removed,
                                           "error_type": type(exc).__name__, "error": str(exc)})
        raise
    receipt = {"format": "managed-material-retirement/1", "at": utc(), "plan": plan,
               "plan_sha256": expected_plan, "previous_selection": expected_selection,
               "source_manifest": current["manifest"], "retired": removed,
               "recovery": "Bodies, occurrences and original transports remain in the managed reference closure.",
               "boundary": "Cooperative local maintenance; no cross-filesystem atomicity or concurrency lock against external editors."}
    raw = js(receipt); key = sha(raw)
    immutable(store.root / "lifecycle" / (key + ".json"), raw)
    successor = {**current, "previous_selection": expected_selection,
                 "lifecycle": [*current.get("lifecycle", []), key],
                 "materializations": [*current.get("materializations", []),
                      *({"prefix": r["prefix"], "state": "retired", "receipt": key} for r in removed)]}
    successor_raw = js(successor); successor_key = sha(successor_raw)
    immutable(store.root / "selections" / (successor_key + ".json"), successor_raw)
    replace_json(store.root / "CURRENT.json", {"selection": successor_key, "path": "selections/" + successor_key + ".json"})
    return {"receipt": key, "selection": successor_key, "retired_materializations": len(removed),
            "retired_occurrence_bytes": sum(r["bytes"] for r in removed), "canonical_objects_removed": 0}


def rebuild(source_path, output, expected_index, expected_selection, account):
    """Regenerate existing identities into a new physical store; never edit source."""
    source_path, output = Path(source_path).resolve(), Path(output).resolve()
    require(not output.exists(), "Representation target must not exist")
    require(source_path not in output.parents and output not in source_path.parents,
            "Representation target and source must be separate roots")
    index_raw = (source_path / "index.json").read_bytes()
    current_raw = (source_path / "CURRENT.json").read_bytes()
    require(sha(index_raw) == expected_index, "Source index differs from received identity")
    require(json.loads(current_raw)["selection"] == expected_selection, "Source selection differs from received identity")
    require(account, "Representation transition needs an owner account")
    source = Store(source_path)
    source_selected_key, source_selected = current_selection(source)
    selection_closure(source, source_selected_key)
    events_raw = (source_path / "events.jsonl").read_bytes() if (source_path / "events.jsonl").exists() else b""
    started = utc(); output.mkdir(parents=True)
    copied_records = []
    for directory in IMMUTABLE_RECORD_DIRS:
        for path in sorted((source_path / directory).rglob("*.json")):
            require(not path.is_symlink(), "Cannot migrate a linked immutable record")
            raw = path.read_bytes(); relative = path.relative_to(source_path)
            immutable(output / relative, raw)
            copied_records.append({"path": relative.as_posix(), "sha256": sha(raw), "bytes": len(raw)})
    target = Store(output, create=True)
    target.event("representation-rebuild-started", {"source": str(source_path), "source_index": expected_index,
                                                    "source_selection": expected_selection, "account": account})
    try:
        # Materialize underlying archive frames before managed exports which
        # contain their encoded views. This preserves the same raw identities
        # without reinstating one physical file for each legacy header.
        archive_order = []
        for key, row in source.objects.items():
            if row["kind"] == "zip-recipe":
                recipe = source.recipe(key)
                names = {m["name"] for m in recipe["members"]}
                managed = "index.json" in names and "EXPORT-MANIFEST.json" in names
                archive_order.append((managed, row["bytes"], key))
        for number, (_, _, key) in enumerate(sorted(archive_order), 1):
            raw = source.materialize(key)
            require(target.ingest(raw, "retained.zip") == key, "Regenerated transport identity differs")
            print(json.dumps({"phase": "rebuild-transports", "completed": number, "total": len(archive_order)}), file=sys.stderr, flush=True)
        for number, key in enumerate(sorted(source.objects), 1):
            if key not in target.objects:
                raw = source.materialize(key)
                require(target.ingest(raw) == key, "Regenerated retained body differs")
            if number % 20000 == 0:
                print(json.dumps({"phase": "rebuild-content", "completed": number, "total": len(source.objects)}), file=sys.stderr, flush=True)
        # Full raw-identity readback covers old internal frame/encoding identities
        # too; they remain addressable even when represented as verified views.
        target.cache.clear(); target.cache_bytes = 0
        for number, key in enumerate(sorted(source.objects), 1):
            raw = target.materialize(key)
            require(len(raw) == source.objects[key]["bytes"], "Rebuilt original byte count differs")
            if number % 20000 == 0:
                print(json.dumps({"phase": "rebuild-readback", "completed": number, "total": len(source.objects)}), file=sys.stderr, flush=True)
        require((source_path / "index.json").read_bytes() == index_raw and
                (source_path / "CURRENT.json").read_bytes() == current_raw,
                "Source store changed during representation regeneration")
        if (source_path / "events.jsonl").exists():
            require((source_path / "events.jsonl").read_bytes() == events_raw, "Source operation history changed")
        producer = target.blob(LOADED_PRODUCER_BYTES)
        source_events = target.blob(events_raw)
        record = {"format": "managed-material-representation-transition/1", "started_at": started,
                  "completed_at": utc(), "source_store": str(source_path), "source_index_sha256": expected_index,
                  "source_index_bytes": len(index_raw), "source_selection": expected_selection,
                  "source_current_sha256": sha(current_raw), "source_object_count": len(source.objects),
                  "source_object_id_set_sha256": sha(js(sorted(source.objects))),
                  "source_objects_exactly_read_back": len(source.objects), "immutable_records": copied_records,
                  "evidence_objects": [producer, source_events], "producer_sha256": producer,
                  "before_payload": source.stats(), "after_payload": target.stats(), "account": account,
                  "physical_change": "Raw content and transport identities retained; ZIP frame bytes packed with exact slice views; recipe JSON physically compressed; immutable metadata bodies reused by verified views.",
                  "source_untouched": True, "source_representation_retired": False,
                  "source_index_evidence": "Exact old index is pinned here, not treated as an immutable view of a mutable index. Retaining its full historical transport is a separate explicit input and measured cost.",
                  "limits": "New store is a received candidate until owner acceptance. No source-store cleanup, native migration, semantic reacceptance or concurrent-editor transaction."}
        record_raw = js(record); transition_key = sha(record_raw)
        immutable(output / "transitions" / (transition_key + ".json"), record_raw)
        successor = {**source_selected, "previous_selection": expected_selection,
                     "representation_transitions": [*source_selected.get("representation_transitions", []), transition_key]}
        selected_raw = js(successor); selected_key = sha(selected_raw)
        immutable(output / "selections" / (selected_key + ".json"), selected_raw)
        target.commit_index()
        replace_json(output / "CURRENT.json", {"selection": selected_key, "path": "selections/" + selected_key + ".json"})
        checked = verify(target)
        result = {"format": "managed-material-rebuild-result/1", "output": str(output),
                  "transition": transition_key, "selection": selected_key, "source_index_sha256": expected_index,
                  "all_original_identities_preserved": len(source.objects), "source_untouched": True,
                  "source_physical_retirement": "pending-owner-receiving", "verified": checked,
                  "before_store_payload": source.stats(), "after_store_payload": target.stats()}
        target.event("representation-rebuild-completed", result)
        return result
    except Exception as exc:
        target.commit_index()
        target.event("representation-rebuild-failed", {"error_type": type(exc).__name__, "error": str(exc),
                                                       "source_untouched": True})
        raise


def retain_artifact(store, path, label, account):
    """Add exact historical evidence without changing the main current artifact."""
    path = Path(path).resolve(strict=True); safe_relative(label)
    require("/" not in label and account, "Retained artifact needs one label and an account")
    before = path_state(path); raw = path.read_bytes()
    require(path_state(path) == before, "Historical input changed while reading")
    previous_key, previous = current_selection(store)
    object_key = store.ingest(raw, path.name)
    require(sha(path.read_bytes()) == object_key, "Historical input changed before retention")
    occurrence = "retained-" + label + "-" + object_key[:16]
    require(not (store.root / "occurrences" / (occurrence + ".json")).exists(), "Retained artifact occurrence already exists")
    producer = store.blob(LOADED_PRODUCER_BYTES)
    manifest = {"format": "managed-material-occurrence/1", "id": occurrence, "started_at": utc(),
                "completed_at": utc(), "spec": {"account": account}, "files": [{"path": label,
                "source_path": str(path), "type": "file", "object": object_key, "bytes": len(raw),
                "mode": before["mode"], "mtime_ns": before["mtime_ns"], "role": "historical-evidence",
                "materialization": "observed-transport-copy"}], "history": [], "current": [],
                "materialization_groups": [], "producer": {"object": producer, "sha256": producer},
                "scope": "Exact explicitly selected historical artifact; main current roles unchanged."}
    manifest_raw = js(manifest); manifest_key = sha(manifest_raw)
    immutable(store.root / "manifests" / (manifest_key + ".json"), manifest_raw)
    immutable(store.root / "occurrences" / (occurrence + ".json"), js({"format": "managed-material-occurrence-pointer/1",
        "id": occurrence, "manifest": manifest_key, "path": "manifests/" + manifest_key + ".json"}))
    selected = {**previous, "previous_selection": previous_key,
                "additional_manifests": [*previous.get("additional_manifests", []), manifest_key]}
    selected_raw = js(selected); selected_key = sha(selected_raw)
    immutable(store.root / "selections" / (selected_key + ".json"), selected_raw)
    store.commit_index()
    replace_json(store.root / "CURRENT.json", {"selection": selected_key, "path": "selections/" + selected_key + ".json"})
    result = {"object": object_key, "bytes": len(raw), "occurrence": occurrence,
              "manifest": manifest_key, "selection": selected_key, "current_roles_unchanged": True}
    store.event("historical-artifact-retained", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("snapshot", "verify", "export", "restore", "get", "stats", "retirement-plan", "retire", "rebuild", "retain"):
        p = sub.add_parser(name)
        if name != "rebuild":
            p.add_argument("--store", required=True, type=Path)
        if name in ("verify", "export", "restore", "retirement-plan"):
            p.add_argument("--occurrence")
        if name in ("export", "restore", "get"):
            p.add_argument("--output", required=True, type=Path)
        if name == "restore":
            p.add_argument("--prefix")
        if name == "get":
            p.add_argument("--object", required=True)
        if name in ("snapshot", "retirement-plan"):
            p.add_argument("--spec", required=True, type=Path)
        if name == "retire":
            p.add_argument("--plan", required=True, type=Path)
            p.add_argument("--expected-plan", required=True)
            p.add_argument("--expected-selection", required=True)
        if name == "rebuild":
            p.add_argument("--source-store", required=True, type=Path)
            p.add_argument("--output", required=True, type=Path)
            p.add_argument("--expected-index", required=True)
            p.add_argument("--expected-selection", required=True)
            p.add_argument("--account", required=True)
        if name == "retain":
            p.add_argument("--path", required=True, type=Path)
            p.add_argument("--label", required=True)
            p.add_argument("--account", required=True)
    args = parser.parse_args()
    if args.command == "rebuild":
        result = rebuild(args.source_store, args.output, args.expected_index, args.expected_selection, args.account)
        print(json.dumps(result, indent=2))
        return
    store = Store(args.store, create=args.command == "snapshot")
    if args.command == "snapshot":
        result = snapshot(store, args.spec)
    elif args.command == "verify":
        result = verify(store, args.occurrence)
    elif args.command == "export":
        result = export(store, args.output, args.occurrence)
    elif args.command == "restore":
        result = restore(store, args.output, args.occurrence, args.prefix)
    elif args.command == "get":
        require(not args.output.exists(), "Preserve existing materialization")
        raw = store.materialize(args.object)
        immutable(args.output, raw)
        result = {"object": args.object, "output": str(args.output), "bytes": len(raw), "verified": True}
    elif args.command == "retirement-plan":
        result = retirement_plan(store, args.spec, args.occurrence)
    elif args.command == "retire":
        result = retire(store, args.plan, args.expected_plan, args.expected_selection)
    elif args.command == "retain":
        result = retain_artifact(store, args.path, args.label, args.account)
    else:
        result = store.stats()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
