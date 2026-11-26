"""Content-addressed object store, exact ZIP recipe ingestion, and verified reconstruction."""
from __future__ import annotations

import collections
import hashlib
import importlib.util
import io
import os
from pathlib import Path, PurePosixPath
import struct
import sys
import zipfile
import zlib


def _load_store_io():
    if __package__:
        from . import store_io as module
        return module
    path = Path(__file__).resolve().with_name("store_io.py")
    key = "_managed_material_store_io_" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    module = sys.modules.get(key)
    if module is None:
        spec = importlib.util.spec_from_file_location(key, path)
        if spec is None or spec.loader is None:
            raise ImportError("Unable to load store_io module: " + str(path))
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(key, None)
            raise
    return module


_io = _load_store_io()
FORMAT = _io.FORMAT
RAW_BODY_KINDS = _io.RAW_BODY_KINDS
IMMUTABLE_RECORD_DIRS = _io.IMMUTABLE_RECORD_DIRS
append_event_jsonl = _io.append_event_jsonl
check_unfollowed_ancestors = _io.check_unfollowed_ancestors
deflate = _io.deflate
immutable = _io.immutable
is_reparse_or_symlink = _io.is_reparse_or_symlink
js = _io.js
loads_json = _io.loads_json
replace_json = _io.replace_json
require = _io.require
safe_relative = _io.safe_relative
sha = _io.sha
store_lock = _io.store_lock

MAX_ZIP_DEPTH = 12
MAX_ZIP_MEMBERS = 200000
MAX_MEMBER_BYTES = 1024 ** 3
MAX_EXPANDED_BYTES = 4 * 1024 ** 3


def _read_store_relative(root: Path, rel_path: str, label: str = "Store object") -> bytes:
    rel = safe_relative(rel_path)
    cur = root
    for part in rel.parts:
        cur = cur / part
        require(not is_reparse_or_symlink(cur), f"{label} must not traverse a symlink or reparse point: {cur}")
    return cur.read_bytes()


class Store:
    def __init__(self, root, create=False):
        self.root = check_unfollowed_ancestors(root, "Store root", include_target=True)
        self._create = create
        self.cache = collections.OrderedDict()
        self.cache_bytes = 0
        self.building = set()
        self.zip_expanded = 0
        self.observations = []
        self.reload(create=create)

    def reload(self, create=None):
        if create is None:
            create = self._create
        self.root = check_unfollowed_ancestors(self.root, "Store root", include_target=True)
        index = self.root / "index.json"
        if index.exists() or is_reparse_or_symlink(index):
            require(not is_reparse_or_symlink(index), "Store index must not be a symlink or reparse point: " + str(index))
            record = loads_json(index.read_bytes())
            require(record["format"] == FORMAT, "Unknown store format")
            self.objects = record["objects"]
        else:
            require(create, "Store index does not exist")
            self.root.mkdir(parents=True, exist_ok=True)
            self.objects = {}
        self.encoded_sources = {row["encoded_sha256"]: key for key, row in self.objects.items()
                                if row["kind"] in RAW_BODY_KINDS and "encoded_sha256" in row}
        self.recipe_sources = {row["recipe_sha256"]: key for key, row in self.objects.items() if row["kind"] == "zip-recipe"}
        self.encoded_recipe_sources = {row["recipe_encoded_sha256"]: key for key, row in self.objects.items()
                                       if row["kind"] == "zip-recipe" and row.get("recipe_encoding") == "raw-deflate"}
        self.record_sources = {}
        for directory in IMMUTABLE_RECORD_DIRS:
            folder = self.root / directory
            if not folder.exists() and not is_reparse_or_symlink(folder):
                continue
            require(
                not is_reparse_or_symlink(folder),
                "Immutable record directory must not be a symlink or reparse point: " + str(folder),
            )
            if not folder.is_dir():
                continue
            for parent, dirs, files in os.walk(folder, followlinks=False):
                dirs.sort()
                files.sort()
                for d in dirs:
                    subdir = Path(parent) / d
                    require(
                        not is_reparse_or_symlink(subdir),
                        "Immutable record directory must not traverse a symlink or reparse point: " + str(subdir),
                    )
                for name in files:
                    if not name.endswith(".json"):
                        continue
                    path = Path(parent) / name
                    require(
                        not is_reparse_or_symlink(path),
                        "Immutable record must not be a symlink or reparse point: " + str(path),
                    )
                    raw = path.read_bytes()
                    ident = sha(raw)
                    if directory != "occurrences":
                        require(path.stem == ident, "Immutable record name differs: " + str(path))
                    self.record_sources.setdefault(ident, path.relative_to(self.root).as_posix())

    def event(self, kind, data):
        append_event_jsonl(self.root, kind, data)

    def commit_index(self):
        with store_lock(self.root):
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
                if path.exists() or is_reparse_or_symlink(path):
                    require(not is_reparse_or_symlink(path), "Immutable metadata must not be a symlink or reparse point")
                    if path.is_file():
                        require(path.read_bytes() == raw, "Immutable metadata differs")
                        record_path = path.relative_to(self.root).as_posix()
                        self.record_sources[key] = record_path
                        break
        if record_path is not None:
            require(_read_store_relative(self.root, record_path, "Immutable record") == raw, "Immutable record body differs")
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
            require(_read_store_relative(self.root, self.objects[recipe_source]["path"], "Persisted recipe") == raw,
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
            encoded = _read_store_relative(self.root, info["path"], "Stored blob")
        elif info["kind"] == "recipe-view" and self.objects[info["source"]].get("recipe_encoding") == "raw-deflate":
            encoded = _read_store_relative(self.root, self.objects[info["source"]]["path"], "Stored recipe")
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
        raw = _read_store_relative(self.root, info["path"], "ZIP recipe")
        if info.get("recipe_encoding") == "raw-deflate":
            require(len(raw) == info["recipe_encoded_bytes"] and sha(raw) == info["recipe_encoded_sha256"],
                    "Encoded ZIP recipe differs: " + key)
            raw = zlib.decompress(raw, -15)
        require(sha(raw) == info["recipe_sha256"], "ZIP recipe differs: " + key)
        return raw

    def recipe(self, key):
        return loads_json(self.recipe_raw(key))

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
            raw = _read_store_relative(self.root, info["record_path"], "Immutable record")
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
