"""Content-addressed object store, exact ZIP recipe ingestion, and verified reconstruction."""

import collections
import io
import os
import struct
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Any

from .config import DEFAULT_LIMITS, FORMAT, IMMUTABLE_RECORD_DIRS, RAW_BODY_KINDS, RetentionLimits
from .store_io import (
    append_event_jsonl,
    check_unfollowed_ancestors,
    check_unfollowed_descendant,
    deflate,
    immutable_below,
    is_reparse_or_symlink,
    js,
    loads_json,
    replace_json,
    require,
    safe_relative,
    sha,
    store_lock,
)

MAX_ZIP_DEPTH = DEFAULT_LIMITS.max_zip_depth
MAX_ZIP_MEMBERS = DEFAULT_LIMITS.max_zip_members
MAX_MEMBER_BYTES = DEFAULT_LIMITS.max_member_bytes
MAX_EXPANDED_BYTES = DEFAULT_LIMITS.max_expanded_bytes

_HASH_INDEXED_RECORD_DIRS = ("manifests", "selections", "lifecycle", "transitions")
_HEX_CHARS = frozenset("0123456789abcdef")


def _read_store_relative(root: Path, rel_path: str, label: str = "Store object") -> bytes:
    """Read an immutable object below a validated store root without quadratic ancestor lstats."""
    rel = safe_relative(rel_path)
    target = check_unfollowed_descendant(root, rel.as_posix(), role=label)
    return target.read_bytes()


class Store:
    """Content-addressed store with verified raw blobs, metadata views, and exact ZIP recipes."""

    def __init__(
        self,
        root: Path | str,
        create: bool = False,
        *,
        limits: RetentionLimits = DEFAULT_LIMITS,
    ) -> None:
        self.root = check_unfollowed_ancestors(root, "Store root", include_target=True)
        self._create = create
        self.limits = limits
        self.cache: collections.OrderedDict[str, bytes] = collections.OrderedDict()
        self.cache_bytes = 0
        self.building: set[str] = set()
        self.zip_expanded = 0
        self.observations: list[dict[str, Any]] = []
        self.objects: dict[str, dict[str, Any]] = {}
        self.encoded_sources: dict[str, str] = {}
        self.recipe_sources: dict[str, str] = {}
        self.encoded_recipe_sources: dict[str, str] = {}
        self.record_sources: dict[str, str] = {}
        self._occurrences_indexed = False
        self.reload(create=create)

    def reload(self, create: bool | None = None) -> None:
        """Reload index.json and source lookup tables without eagerly scanning historical JSON
        trees.
        """
        if create is None:
            create = self._create

        self.root = check_unfollowed_ancestors(self.root, "Store root", include_target=True)
        index = self.root / "index.json"
        if index.exists() or is_reparse_or_symlink(index):
            require(
                not is_reparse_or_symlink(index),
                "Store index must not be a symlink or reparse point: " + str(index),
            )
            record = loads_json(index.read_bytes())
            require(record["format"] == FORMAT, "Unknown store format")
            self.objects = record["objects"]
        else:
            require(create, "Store index does not exist")
            self.root.mkdir(parents=True, exist_ok=True)
            self.objects = {}

        self.encoded_sources = {
            row["encoded_sha256"]: key
            for key, row in self.objects.items()
            if row["kind"] in RAW_BODY_KINDS and "encoded_sha256" in row
        }
        self.recipe_sources = {
            row["recipe_sha256"]: key
            for key, row in self.objects.items()
            if row["kind"] == "zip-recipe"
        }
        self.encoded_recipe_sources = {
            row["recipe_encoded_sha256"]: key
            for key, row in self.objects.items()
            if row["kind"] == "zip-recipe" and row.get("recipe_encoding") == "raw-deflate"
        }
        self.record_sources = {}
        self._occurrences_indexed = False

    def _index_occurrences_once(self) -> None:
        """Lazily index occurrences/*.json only when blob() checks for a record-view match."""
        if self._occurrences_indexed:
            return
        self._occurrences_indexed = True

        folder = self.root / "occurrences"
        if not folder.exists() and not is_reparse_or_symlink(folder):
            return
        require(
            not is_reparse_or_symlink(folder),
            "Immutable record directory must not be a symlink or reparse point: " + str(folder),
        )
        if not folder.is_dir():
            return

        for parent, dirs, files in os.walk(folder, followlinks=False):
            dirs.sort()
            files.sort()
            for dirname in dirs:
                subdir = Path(parent) / dirname
                require(
                    not is_reparse_or_symlink(subdir),
                    "Immutable record directory must not traverse a symlink or reparse point: "
                    + str(subdir),
                )
            for name in files:
                if not name.endswith(".json"):
                    continue
                path = Path(parent) / name
                require(
                    not is_reparse_or_symlink(path),
                    "Immutable record must not be a symlink or reparse point: " + str(path),
                )
                ident = sha(path.read_bytes())
                self.record_sources.setdefault(ident, path.relative_to(self.root).as_posix())

    def _find_record_source(self, key: str, raw: bytes) -> str | None:
        """Look up an existing immutable metadata record matching key without eager directory
        walks.
        """
        record_path = self.record_sources.get(key)
        if record_path is not None:
            return record_path

        for directory in _HASH_INDEXED_RECORD_DIRS:
            folder = self.root / directory
            if is_reparse_or_symlink(folder):
                raise ValueError(
                    "Immutable metadata must not be a symlink or reparse point: " + str(folder),
                )
            path = folder / (key + ".json")
            if path.exists() or is_reparse_or_symlink(path):
                require(
                    not is_reparse_or_symlink(path),
                    "Immutable metadata must not be a symlink or reparse point",
                )
                if path.is_file():
                    require(path.read_bytes() == raw, "Immutable metadata differs")
                    record_path = path.relative_to(self.root).as_posix()
                    self.record_sources[key] = record_path
                    return record_path

        self._index_occurrences_once()
        return self.record_sources.get(key)

    def event(self, kind: str, data: dict[str, Any]) -> None:
        """Append one durable JSONL event record for this store."""
        append_event_jsonl(self.root, kind, data)

    def commit_index(self) -> None:
        """Atomically persist index.json under the exclusive cooperative store lock."""
        with store_lock(self.root):
            replace_json(self.root / "index.json", {"format": FORMAT, "objects": self.objects})

    def clear_cache(self) -> None:
        """Evict all decoded object bytes held in the in-memory LRU cache."""
        self.cache.clear()
        self.cache_bytes = 0

    def cache_put(self, key: str, raw: bytes) -> None:
        """Store raw bytes in the bounded LRU cache if below the per-object limit."""
        if len(raw) > self.limits.max_cache_object_bytes:
            return
        if key in self.cache:
            self.cache.move_to_end(key)
            return

        self.cache[key] = raw
        self.cache_bytes += len(raw)
        while self.cache_bytes > self.limits.max_cache_total_bytes:
            _, old = self.cache.popitem(last=False)
            self.cache_bytes -= len(old)

    def _register_raw_view(self, key: str, raw: bytes, row: dict[str, Any]) -> str:
        """Attach raw-deflate metadata for a raw-body view using a single SHA-256 pass."""
        encoded = deflate(raw)
        encoded_sha = sha(encoded)
        self.objects[key] = {
            **row,
            "bytes": len(raw),
            "encoding": "raw-deflate",
            "encoded_sha256": encoded_sha,
            "encoded_bytes": len(encoded),
        }
        self.encoded_sources[encoded_sha] = key
        self.cache_put(key, raw)
        return key

    def blob(self, raw: bytes) -> str:
        """Store or deduplicate raw bytes as a verified view or persisted compressed blob."""
        key = sha(raw)
        if key in self.objects:
            require(self.materialize(key) == raw, "Existing body differs: " + key)
            return key

        recipe_source = self.recipe_sources.get(key)
        if recipe_source is not None and recipe_source not in self.building:
            require(self.recipe_raw(recipe_source) == raw, "Immutable recipe body differs")
            return self._register_raw_view(
                key,
                raw,
                {"kind": "recipe-view", "source": recipe_source, "dependencies": [recipe_source]},
            )

        record_path = self._find_record_source(key, raw)
        if record_path is not None:
            require(
                _read_store_relative(self.root, record_path, "Immutable record") == raw,
                "Immutable record body differs",
            )
            return self._register_raw_view(
                key,
                raw,
                {"kind": "record-view", "record_path": record_path, "record_sha256": key},
            )

        if key in self.encoded_sources:
            source = self.encoded_sources[key]
            require(self.encoded_blob(source) == raw, "Persisted encoded body differs")
            self.objects[key] = {
                "kind": "encoded-view",
                "bytes": len(raw),
                "source": source,
                "dependencies": [source],
            }
            self.cache_put(key, raw)
            return key

        recipe_source = self.encoded_recipe_sources.get(key)
        if recipe_source is not None and recipe_source not in self.building:
            logical = self.recipe_raw(recipe_source)
            require(
                _read_store_relative(
                    self.root, self.objects[recipe_source]["path"], "Persisted recipe"
                )
                == raw,
                "Persisted recipe encoding differs",
            )
            source = self.blob(logical)
            entry = self.objects[source]
            if entry["kind"] in RAW_BODY_KINDS and entry.get("encoded_sha256") == key:
                require(
                    source != key and self.encoded_blob(source) == raw,
                    "Recipe encoding view differs",
                )
                self.objects[key] = {
                    "kind": "encoded-view",
                    "bytes": len(raw),
                    "source": source,
                    "dependencies": [source],
                }
                self.cache_put(key, raw)
                return key

        if key not in self.objects:
            encoded = deflate(raw)
            encoded_sha = sha(encoded)
            relative = "objects/" + key[:2] + "/" + key[2:] + ".deflate"
            immutable_below(self.root, relative, encoded)
            self.objects[key] = {
                "kind": "blob",
                "bytes": len(raw),
                "path": relative,
                "encoding": "raw-deflate",
                "encoded_sha256": encoded_sha,
                "encoded_bytes": len(encoded),
            }
            self.encoded_sources[encoded_sha] = key

        self.cache_put(key, raw)
        return key

    def encoded_blob(self, key: str, stack: tuple[str, ...] = ()) -> bytes:
        """Read or derive the verified raw-deflate encoding for a raw-body object."""
        info = self.objects[key]
        require(info["kind"] in RAW_BODY_KINDS, "No body encoding for a virtual transport")
        if info["kind"] == "blob":
            encoded = _read_store_relative(self.root, info["path"], "Stored blob")
        elif (
            info["kind"] == "recipe-view"
            and self.objects[info["source"]].get("recipe_encoding") == "raw-deflate"
        ):
            encoded = _read_store_relative(
                self.root,
                self.objects[info["source"]]["path"],
                "Stored recipe",
            )
        else:
            encoded = deflate(self.materialize(key, stack))

        require(
            len(encoded) == info["encoded_bytes"] and sha(encoded) == info["encoded_sha256"],
            "Encoded body differs: " + key,
        )
        return encoded

    def slice_view(self, raw: bytes, source: str, start: int, end: int) -> str:
        """Register an exact byte slice view within an existing framing blob."""
        key = sha(raw)
        if key in self.objects:
            require(self.materialize(key) == raw, "Existing framing identity differs")
            return key

        require(key != source and len(raw) == end - start, "Invalid framing slice")
        return self._register_raw_view(
            key,
            raw,
            {
                "kind": "slice-view",
                "source": source,
                "start": start,
                "end": end,
                "dependencies": [source],
            },
        )

    def ingest(self, raw: bytes, hint: str = "", depth: int = 0) -> str:
        """Ingest raw content, recognizing managed deflate views and exact ZIP transports."""
        key = sha(raw)
        if key in self.objects:
            require(self.materialize(key) == raw, "Existing material differs: " + key)
            return key

        parts = PurePosixPath(hint).parts
        if len(parts) >= 3 and parts[-3] == "objects" and parts[-1].endswith(".deflate"):
            expected = parts[-2] + parts[-1][:-8]
            if len(expected) == 64 and all(ch in _HEX_CHARS for ch in expected):
                try:
                    decoded = zlib.decompress(raw, -15)
                except zlib.error:
                    decoded = None
                if decoded is not None and sha(decoded) == expected:
                    require(
                        len(decoded) <= self.limits.max_member_bytes,
                        "Managed encoded body size limit",
                    )
                    self.blob(decoded)
                    return self.blob(raw)

        maybe_zip = hint.lower().endswith(".zip") or raw.startswith((b"PK\x03\x04", b"PK\x05\x06"))
        if maybe_zip and zipfile.is_zipfile(io.BytesIO(raw)):
            return self.ingest_zip(raw, key, depth)

        if hint.lower().endswith(".zip"):
            self.observations.append(
                {"kind": "invalid-zip-retained-as-bytes", "hint": hint, "object": key},
            )
        return self.blob(raw)

    def _pack_zip_framing(
        self,
        raw: bytes,
        ordered: list[tuple[int, zipfile.ZipInfo]],
    ) -> tuple[list[str], list[bytes]]:
        """Pack all ZIP header/trailer framing slices into one domain-separated blob."""
        frame_chunks: list[bytes] = []
        previous_end = 0

        for _, info in ordered:
            off = info.header_offset
            require(off >= previous_end and off + 30 <= len(raw), "Invalid framing interval")
            require(raw[off : off + 4] == b"PK\x03\x04", "Local ZIP signature differs")
            name_len, extra_len = struct.unpack_from("<HH", raw, off + 26)
            start = off + 30 + name_len + extra_len
            end = start + info.compress_size
            require(previous_end <= start <= end <= len(raw), "Invalid framing extent")
            frame_chunks.append(raw[previous_end:start])
            previous_end = end

        tails = (
            [raw[: len(raw) // 2], raw[len(raw) // 2 :]]
            if previous_end == 0
            else [raw[previous_end:]]
        )
        frame_chunks.extend(tails)

        domain = b"managed-material-zip-framing/1\x00"
        framing = self.blob(domain + b"".join(frame_chunks))
        frame_keys: list[str] = []
        frame_position = len(domain)
        for fragment in frame_chunks:
            frame_keys.append(
                self.slice_view(fragment, framing, frame_position, frame_position + len(fragment)),
            )
            frame_position += len(fragment)
        return frame_keys, tails

    def _encode_member_part(
        self,
        info: zipfile.ZipInfo,
        content: str | None,
        body: bytes | None,
        compressed: bytes,
        compressed_sha: str,
        problem: str | None,
        dependencies: set[str],
    ) -> dict[str, Any]:
        """Select an exact deterministic replay recipe for one compressed ZIP member."""
        part: dict[str, Any] | None = None
        if content is not None and info.compress_type == zipfile.ZIP_STORED and body == compressed:
            part = {"kind": "stored", "content": content}
        elif (
            content is not None and body is not None and info.compress_type == zipfile.ZIP_DEFLATED
        ):
            entry = self.objects[content]
            if entry["kind"] in RAW_BODY_KINDS and entry["encoded_sha256"] == compressed_sha:
                part = {"kind": "blob-encoding", "content": content}
            else:
                for level in (6, 9, 1, 2, 3, 4, 5, 7, 8, 0):
                    if deflate(body, level=level) == compressed:
                        part = {
                            "kind": "deflate-recipe",
                            "content": content,
                            "level": level,
                            "mem_level": 8,
                            "strategy": 0,
                            "verified_encoder": {
                                "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
                                "zlib_compile": zlib.ZLIB_VERSION,
                            },
                        }
                        break

        if part is None:
            encoded_id = self.blob(compressed)
            dependencies.add(encoded_id)
            part = {
                "kind": "encoded-fallback",
                "object": encoded_id,
                "reason": problem or "No exact supported compression recipe matched",
                "content": content,
            }

        part.update({"sha256": compressed_sha, "bytes": len(compressed)})
        return part

    def _ingest_zip_member(
        self,
        archive: zipfile.ZipFile,
        raw: bytes,
        ordinal: int,
        info: zipfile.ZipInfo,
        cursor: int,
        frame: str,
        depth: int,
        parts: list[dict[str, Any]],
        members: list[dict[str, Any]],
        dependencies: set[str],
        methods: collections.Counter[str],
    ) -> int:
        """Ingest one ordered ZIP member and append its framing + body recipe entries."""
        require(info.file_size <= self.limits.max_member_bytes, "ZIP member size limit")
        self.zip_expanded += info.file_size
        require(self.zip_expanded <= self.limits.max_expanded_bytes, "Expanded ZIP byte limit")

        off = info.header_offset
        require(off >= cursor and off + 30 <= len(raw), "Overlapping or invalid local header")
        require(raw[off : off + 4] == b"PK\x03\x04", "Local ZIP signature differs")
        name_len, extra_len = struct.unpack_from("<HH", raw, off + 26)
        start = off + 30 + name_len + extra_len
        end = start + info.compress_size
        require(cursor <= start <= end <= len(raw), "Invalid compressed member interval")

        dependencies.add(frame)
        parts.append({"kind": "frame", "object": frame})

        compressed = raw[start:end]
        compressed_sha = sha(compressed)
        content: str | None = None
        body: bytes | None = None
        problem: str | None = None
        try:
            body = archive.read(info)
            require(len(body) == info.file_size, "ZIP decoded size differs")
            content = self.ingest(body, info.filename, depth + 1)
            dependencies.add(content)
        except (NotImplementedError, RuntimeError, zipfile.BadZipFile) as exc:
            problem = type(exc).__name__ + ": " + str(exc)

        part = self._encode_member_part(
            info,
            content,
            body,
            compressed,
            compressed_sha,
            problem,
            dependencies,
        )
        methods[part["kind"]] += 1
        part_index = len(parts)
        parts.append(part)

        member_row: dict[str, Any] = {
            "ordinal": ordinal,
            "name": info.filename,
            "is_directory": info.is_dir(),
            "bytes": info.file_size,
            "compressed_bytes": info.compress_size,
            "compression": info.compress_type,
            "flags": info.flag_bits,
            "crc32": info.CRC,
            "content": content,
            "part": part_index,
            "semantic_content": "retained" if content is not None else "unavailable",
        }
        if problem:
            member_row["decode_error"] = problem
        members.append(member_row)
        return end

    def _write_zip_recipe(
        self,
        key: str,
        raw: bytes,
        parts: list[dict[str, Any]],
        members: list[dict[str, Any]],
        dependencies: set[str],
        methods: collections.Counter[str],
    ) -> None:
        """Persist a compressed ZIP recipe and register its object metadata."""
        sorted_deps = sorted(dependencies)
        recipe = {
            "format": "managed-material-zip/1",
            "sha256": key,
            "bytes": len(raw),
            "parts": parts,
            "members": sorted(members, key=lambda row: row["ordinal"]),
            "dependencies": sorted_deps,
            "member_encodings": dict(methods),
        }
        recipe_raw = js(recipe)
        recipe_sha = sha(recipe_raw)
        recipe_encoded = deflate(recipe_raw)
        recipe_encoded_sha = sha(recipe_encoded)
        relative = "recipes/" + key + ".json.deflate"
        immutable_below(self.root, relative, recipe_encoded)

        self.objects[key] = {
            "kind": "zip-recipe",
            "bytes": len(raw),
            "path": relative,
            "recipe_sha256": recipe_sha,
            "recipe_bytes": len(recipe_raw),
            "recipe_encoding": "raw-deflate",
            "recipe_encoded_sha256": recipe_encoded_sha,
            "recipe_encoded_bytes": len(recipe_encoded),
            "dependencies": sorted_deps,
            "member_encodings": dict(methods),
            "members": len(members),
        }
        self.recipe_sources[recipe_sha] = key
        self.encoded_recipe_sources[recipe_encoded_sha] = key

    def ingest_zip(self, raw: bytes, key: str, depth: int) -> str:
        """Decompose a ZIP archive into exact byte-reproducible framing and member recipes."""
        require(depth < self.limits.max_zip_depth, "ZIP depth limit; no selection published")
        require(key not in self.building, "Cyclic transport identity")
        self.building.add(key)

        try:
            parts: list[dict[str, Any]] = []
            members: list[dict[str, Any]] = []
            dependencies: set[str] = set()
            methods: collections.Counter[str] = collections.Counter()

            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                infos = archive.infolist()
                require(len(infos) <= self.limits.max_zip_members, "ZIP member limit")
                ordered = sorted(enumerate(infos), key=lambda pair: pair[1].header_offset)
                frame_keys, tails = self._pack_zip_framing(raw, ordered)
                frame_iter = iter(frame_keys)

                cursor = 0
                for ordinal, info in ordered:
                    cursor = self._ingest_zip_member(
                        archive,
                        raw,
                        ordinal,
                        info,
                        cursor,
                        next(frame_iter),
                        depth,
                        parts,
                        members,
                        dependencies,
                        methods,
                    )

                for _ in tails:
                    trailer = next(frame_iter)
                    dependencies.add(trailer)
                    parts.append({"kind": "frame", "object": trailer})

            self._write_zip_recipe(key, raw, parts, members, dependencies, methods)
        finally:
            self.building.discard(key)

        require(self.materialize(key) == raw, "Immediate exact ZIP reconstruction differs: " + key)
        self.cache_put(key, raw)
        return key

    def recipe_raw(self, key: str) -> bytes:
        """Read and verify the canonical JSON bytes of a persisted ZIP recipe."""
        info = self.objects[key]
        raw = _read_store_relative(self.root, info["path"], "ZIP recipe")
        if info.get("recipe_encoding") == "raw-deflate":
            require(
                len(raw) == info["recipe_encoded_bytes"]
                and sha(raw) == info["recipe_encoded_sha256"],
                "Encoded ZIP recipe differs: " + key,
            )
            raw = zlib.decompress(raw, -15)
        require(sha(raw) == info["recipe_sha256"], "ZIP recipe differs: " + key)
        return raw

    def recipe(self, key: str) -> dict[str, Any]:
        """Parse a verified ZIP recipe document."""
        return loads_json(self.recipe_raw(key))

    def _materialize_zip_recipe(self, key: str, stack: tuple[str, ...]) -> bytes:
        """Reconstruct exact ZIP transport bytes from a verified zip-recipe."""
        buf = io.BytesIO()
        next_stack = (*stack, key)

        for part in self.recipe(key)["parts"]:
            kind = part["kind"]
            if kind in ("frame", "encoded-fallback"):
                fragment = self.materialize(part["object"], next_stack)
            elif kind == "blob-encoding":
                fragment = self.encoded_blob(part["content"], next_stack)
            elif kind == "stored":
                fragment = self.materialize(part["content"], next_stack)
            elif kind == "deflate-recipe":
                fragment = deflate(
                    self.materialize(part["content"], next_stack),
                    part["level"],
                    part["mem_level"],
                    part["strategy"],
                )
            else:
                raise ValueError("Unknown transport part kind: " + kind)

            if kind != "frame":
                require(
                    len(fragment) == part["bytes"] and sha(fragment) == part["sha256"],
                    "Exact compressed replay differs; original encoder may be required: " + key,
                )
            buf.write(fragment)

        return buf.getvalue()

    def materialize(self, key: str, stack: tuple[str, ...] = ()) -> bytes:
        """Reconstruct and hash-verify the exact original bytes for object key."""
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]

        require(key not in stack, "Cyclic content recipe")
        info = self.objects[key]
        kind = info["kind"]

        if kind == "blob":
            raw = zlib.decompress(self.encoded_blob(key), -15)
        elif kind == "slice-view":
            source = self.materialize(info["source"], (*stack, key))
            require(
                0 <= info["start"] <= info["end"] <= len(source),
                "Framing slice is outside its body",
            )
            raw = source[info["start"] : info["end"]]
        elif kind == "record-view":
            relative = safe_relative(info["record_path"])
            require(
                relative.parts[0] in IMMUTABLE_RECORD_DIRS,
                "Metadata view must name an immutable record",
            )
            raw = _read_store_relative(self.root, info["record_path"], "Immutable record")
            require(sha(raw) == info["record_sha256"], "Retained immutable record differs")
        elif kind == "recipe-view":
            require(info["source"] not in stack and info["source"] != key, "Cyclic recipe view")
            raw = self.recipe_raw(info["source"])
        elif kind == "encoded-view":
            raw = self.encoded_blob(info["source"], (*stack, key))
        elif kind == "zip-recipe":
            raw = self._materialize_zip_recipe(key, stack)
        else:
            raise ValueError("Unknown object kind")

        require(len(raw) == info["bytes"] and sha(raw) == key, "Recovered content differs: " + key)
        self.cache_put(key, raw)
        return raw

    def closure(self, roots: Any) -> set[str]:
        """Return the acyclic transitive dependency set of object keys."""
        todo: list[tuple[str, bool]] = [(key, False) for key in roots]
        found: set[str] = set()
        active: set[str] = set()

        while todo:
            key, leaving = todo.pop()
            if leaving:
                active.remove(key)
                found.add(key)
                continue
            if key in found:
                continue

            require(key not in active, "Cyclic content dependency")
            require(key in self.objects, "Absent dependency: " + key)
            active.add(key)
            todo.append((key, True))
            todo.extend(
                (dependency, False) for dependency in self.objects[key].get("dependencies", [])
            )

        return found

    def stats(self, keys: Any = None) -> dict[str, Any]:
        """Compute object-kind counts and payload byte totals for keys (or all store objects)."""
        selected_keys = set(self.objects) if keys is None else set(keys)
        encodings: collections.Counter[str] = collections.Counter()
        kinds: collections.Counter[str] = collections.Counter()

        for key in selected_keys:
            record = self.objects[key]
            kinds[record["kind"]] += 1
            encodings.update(record.get("member_encodings", {}))

        return {
            "object_count": len(selected_keys),
            "kinds": dict(kinds),
            "zip_member_encodings": dict(encodings),
            "persisted_payload_bytes": sum(
                (self.root / self.objects[x]["path"]).stat().st_size
                for x in selected_keys
                if "path" in self.objects[x]
            ),
            "canonical_blob_bytes": sum(
                self.objects[x]["bytes"] for x in selected_keys if self.objects[x]["kind"] == "blob"
            ),
            "virtual_zip_bytes": sum(
                self.objects[x]["bytes"]
                for x in selected_keys
                if self.objects[x]["kind"] == "zip-recipe"
            ),
            "virtual_encoded_bytes": sum(
                self.objects[x]["bytes"]
                for x in selected_keys
                if self.objects[x]["kind"] == "encoded-view"
            ),
        }
