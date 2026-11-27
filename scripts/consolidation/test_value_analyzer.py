#!/usr/bin/env python3
"""Pure-stdlib AST analyzer for semantic test value, mock boundaries, overlap, and unknowns."""

import argparse
import ast
import hashlib
import json
import os
import stat
import sys
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
for _entry in (str(SRC_ROOT), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from managed_ai_information.core.ownership import relative_path  # noqa: E402
from managed_ai_information.core.storage import (  # noqa: E402
    MAX_BYTES,
    is_ordinary_dir_stat,
    read_bytes,
)
from managed_ai_information.platform import (  # noqa: E402
    check_ordinary_ancestors,
    is_ordinary_file_stat,
)
from scripts.consolidation._common import dump_json, git_head_sha, slugify_id  # noqa: E402

TEST_VALUE_REPORT_FORMAT = "managed-ai-test-value-report/1"

_MOCK_CALL_NAMES: frozenset[str] = frozenset(
    {
        "Mock",
        "MagicMock",
        "AsyncMock",
        "NonCallableMock",
        "PropertyMock",
        "create_autospec",
        "patch",
    }
)
_FAKE_CLASS_PREFIXES: tuple[str, ...] = ("_Fake", "Fake", "_Mock", "Mock", "_Stub", "Stub", "Dummy")
_MOCK_SERVER_BASES: frozenset[str] = frozenset(
    {"BaseHTTPRequestHandler", "SimpleHTTPRequestHandler"}
)
_REAL_BOUNDARY_NAMES: frozenset[str] = frozenset(
    {
        "TemporaryDirectory",
        "mkdtemp",
        "System",
        "EmbeddedFileBackend",
        "EphemeralMembusDaemon",
        "subprocess",
    }
)
_REAL_BOUNDARY_METHODS: frozenset[str] = frozenset(
    {"initialize", "write_text", "write_bytes", "mkdir", "run", "Popen"}
)
_ASSERT_RAISES_METHODS: frozenset[str] = frozenset(
    {"assertRaises", "assertRaisesRegex", "assertWarns", "assertWarnsRegex", "raises"}
)


def _module_record_id(module_name: str) -> str:
    parts = module_name.split(".")
    trimmed = ".".join(parts[1:]) if len(parts) > 1 else parts[0]
    return slugify_id("mod", trimmed)


def _unknown_record_id(module_name: str) -> str:
    parts = module_name.split(".")
    trimmed = ".".join(parts[1:]) if len(parts) > 1 else parts[0]
    return slugify_id("unknown-cov", trimmed)


def _suite_record_id(rel_path: str) -> str:
    stem = rel_path.removesuffix(".py")
    if stem.startswith("tests/"):
        stem = stem.removeprefix("tests/")
    return slugify_id("test", stem)


def _iter_ordinary_py_files(root: Path, base_dir: Path) -> list[tuple[Path, str]]:
    """Yield sorted (resolved_path, posix_rel_path) for ordinary .py files under base_dir."""
    checked_root = root.resolve(strict=True)
    checked_base = check_ordinary_ancestors(
        base_dir, expect_dir=True, error_prefix="ordinary dir"
    ).resolve(strict=True)
    if not checked_base.is_relative_to(checked_root):
        raise ValueError(f"source directory escapes repo root: {base_dir}")
    base_rel = checked_base.relative_to(checked_root).as_posix()
    results: list[tuple[Path, str]] = []
    stack: list[tuple[Path, str]] = [(checked_base, "" if base_rel == "." else base_rel)]
    while stack:
        cur, cur_rel = stack.pop()
        with os.scandir(cur) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            name = entry.name
            if name == "__pycache__" or name.startswith("."):
                continue
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"ordinary non-symlink path required: {entry.path}")
            child_path = Path(entry.path)
            child_rel = f"{cur_rel}/{name}" if cur_rel else name
            if stat.S_ISDIR(info.st_mode):
                if not is_ordinary_dir_stat(info):
                    raise ValueError(f"ordinary dir required: {entry.path}")
                stack.append((child_path, child_rel))
                continue
            if not name.endswith(".py"):
                continue
            if not is_ordinary_file_stat(info):
                raise ValueError(f"ordinary Python file required: {entry.path}")
            results.append((child_path, child_rel))
    results.sort(key=lambda pair: pair[1])
    return results


def _iter_stmts(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Iterate statement nodes without descending into expression subtrees."""
    stack: list[list[ast.stmt]] = [body]
    while stack:
        for stmt in stack.pop():
            yield stmt
            for attr in ("body", "orelse", "finalbody"):
                sub = getattr(stmt, attr, None)
                if isinstance(sub, list) and sub:
                    stack.append(sub)
            handlers = getattr(stmt, "handlers", None)
            if isinstance(handlers, list):
                for handler in handlers:
                    if handler.body:
                        stack.append(handler.body)
            cases = getattr(stmt, "cases", None)
            if isinstance(cases, list):
                for case in cases:
                    if case.body:
                        stack.append(case.body)


def _path_to_module_name(pkg_dir: Path, file_path: Path, pkg_name: str) -> str:
    rel_parts = list(file_path.relative_to(pkg_dir).parts)
    if rel_parts[-1] == "__init__.py":
        rel_parts = rel_parts[:-1]
    else:
        rel_parts[-1] = rel_parts[-1].removesuffix(".py")
    return ".".join([pkg_name, *rel_parts]) if rel_parts else pkg_name


def _resolve_relative_import(
    current_mod: str, is_pkg_init: bool, level: int, module: str | None
) -> str:
    parts = current_mod.split(".")
    base = parts if is_pkg_init else parts[:-1]
    drop = max(level - 1, 0)
    prefix = base[: len(base) - drop] if drop <= len(base) else []
    return ".".join([*prefix, *module.split(".")]) if module else ".".join(prefix)


_RawImport = tuple[bool, int, str | None, tuple[tuple[str, str | None], ...]]


def _collect_raw_imports(tree: ast.Module) -> list[_RawImport]:
    raw: list[_RawImport] = []
    for stmt in _iter_stmts(tree.body):
        if isinstance(stmt, ast.Import):
            raw.append((False, 0, None, tuple((a.name, a.asname) for a in stmt.names)))
        elif isinstance(stmt, ast.ImportFrom):
            raw.append(
                (True, stmt.level, stmt.module, tuple((a.name, a.asname) for a in stmt.names))
            )
    return raw


def _resolve_raw_module_imports(
    mod_name: str,
    is_pkg_init: bool,
    raw_imports: list[_RawImport],
    known_modules: set[str],
) -> tuple[set[str], dict[tuple[str, str], str]]:
    """Return (internal_imports, facade_reexports) from lightweight raw import tuples."""
    internal: set[str] = set()
    reexports: dict[tuple[str, str], str] = {}
    for is_from, level, module, names in raw_imports:
        if not is_from:
            for name, _ in names:
                if name in known_modules:
                    internal.add(name)
            continue
        base_mod = (
            _resolve_relative_import(mod_name, is_pkg_init, level, module)
            if level > 0
            else (module or "")
        )
        for name, asname in names:
            if name == "*":
                continue
            exported = asname or name
            candidate = f"{base_mod}.{name}" if base_mod else name
            if candidate in known_modules:
                internal.add(candidate)
                reexports[(mod_name, exported)] = candidate
            elif base_mod in known_modules:
                internal.add(base_mod)
                reexports[(mod_name, exported)] = base_mod
    internal.discard(mod_name)
    return internal, reexports


def extract_facade_reexports(
    mod_name: str,
    is_pkg_init: bool,
    tree: ast.Module,
    known_modules: set[str],
) -> dict[tuple[str, str], str]:
    """Return 1-hop facade re-exports for one production module AST."""
    _, reexports = _resolve_raw_module_imports(
        mod_name, is_pkg_init, _collect_raw_imports(tree), known_modules
    )
    return reexports


def has_executable_statements(tree: ast.Module) -> bool:
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            continue
        return True
    return False


def discover_production_modules(
    repo_root: Path,
    package_rel: str = "src/managed_ai_information",
) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], str]]:
    """Discover ordinary production modules, internal imports, and 1-hop facade re-exports."""
    checked_repo = check_ordinary_ancestors(
        repo_root, expect_dir=True, error_prefix="ordinary repository root required"
    ).resolve(strict=True)
    safe_pkg_rel = relative_path(package_rel)
    pkg_dir = check_ordinary_ancestors(
        checked_repo / safe_pkg_rel,
        expect_dir=True,
        error_prefix=f"ordinary package directory required ({safe_pkg_rel})",
    ).resolve(strict=True)
    if not pkg_dir.is_relative_to(checked_repo):
        raise ValueError(f"package_dir escapes repository root: {safe_pkg_rel}")

    pkg_name = pkg_dir.name
    modules: dict[str, dict[str, Any]] = {}
    extracted: dict[str, tuple[list[_RawImport], bool, bool]] = {}

    for resolved, rel_posix in _iter_ordinary_py_files(checked_repo, pkg_dir):
        mod_name = _path_to_module_name(pkg_dir, resolved, pkg_name)
        text = read_bytes(resolved, MAX_BYTES).decode("utf-8")
        tree = ast.parse(text, filename=rel_posix)
        is_init = resolved.name == "__init__.py"
        is_facade = is_init or mod_name.endswith(".domain_api")
        modules[mod_name] = {
            "module": mod_name,
            "module_id": _module_record_id(mod_name),
            "path": rel_posix,
            "lines": len(text.splitlines()),
            "is_facade": is_facade,
            "has_executable_code": has_executable_statements(tree),
            "internal_imports": [],
        }
        extracted[mod_name] = (_collect_raw_imports(tree), is_init, is_facade)
        del tree

    known_names = set(modules)
    reexports: dict[tuple[str, str], str] = {}
    for mod_name, (raw_imports, is_init, is_facade) in extracted.items():
        internal, mod_reexports = _resolve_raw_module_imports(
            mod_name, is_init, raw_imports, known_names
        )
        modules[mod_name]["internal_imports"] = sorted(internal)
        if is_facade:
            reexports.update(mod_reexports)
    return modules, reexports


def _dotted_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _collect_import_ref(
    node: ast.Import | ast.ImportFrom,
    known_modules: set[str],
    reexports: dict[tuple[str, str], str],
    helper_map: dict[str, set[str]],
    found: set[str],
    symbols: set[str],
) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name in known_modules:
                found.add(alias.name)
            if alias.name in helper_map:
                found.update(helper_map[alias.name])
    elif node.module:
        mod = node.module
        if mod in helper_map:
            found.update(helper_map[mod])
        for alias in node.names:
            sub_mod = f"{mod}.{alias.name}"
            if sub_mod in known_modules:
                found.add(sub_mod)
            elif (mod, alias.name) in reexports:
                found.add(mod)
                found.add(reexports[(mod, alias.name)])
                symbols.add(f"sym:{alias.name}")
            elif mod in known_modules:
                found.add(mod)
                symbols.add(f"sym:{alias.name}")
            if sub_mod in helper_map:
                found.update(helper_map[sub_mod])


def _resolve_prod_refs(
    tree: ast.Module,
    known_modules: set[str],
    path_to_mod: dict[str, str],
    reexports: dict[tuple[str, str], str],
    helper_map: dict[str, set[str]],
) -> tuple[set[str], set[str]]:
    """Return (exercised_prod_modules, imported_prod_symbols) from AST imports & CLI strings."""
    found: set[str] = set()
    symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            _collect_import_ref(node, known_modules, reexports, helper_map, found, symbols)
        elif (
            isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) >= 5
        ):
            val = node.value.strip().replace("\\", "/")
            if val in known_modules:
                found.add(val)
            elif val.endswith(".py") and val in path_to_mod:
                found.add(path_to_mod[val])
    return found, symbols


def _extract_raised_exceptions(call: ast.Call) -> list[str]:
    if not call.args:
        return []
    first = call.args[0]
    if isinstance(first, ast.Tuple):
        names = [_dotted_name(elt).split(".")[-1] for elt in first.elts]
        return [f"raises:{n}" for n in names if n]
    name = _dotted_name(first).split(".")[-1]
    return [f"raises:{name}"] if name else []


def _extract_patch_target(call: ast.Call, callee: str) -> str | None:
    if callee.endswith("patch.object") and len(call.args) >= 2:
        obj_name = _dotted_name(call.args[0]).split(".")[-1]
        arg1 = call.args[1]
        attr = arg1.value if isinstance(arg1, ast.Constant) and isinstance(arg1.value, str) else "?"
        return f"patch:{obj_name}.{attr}" if obj_name else f"patch:{attr}"
    if callee == "patch" or callee.endswith((".patch", ".patch.dict")):
        if (
            call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        ):
            return f"patch:{call.args[0].value}"
        return "patch:dynamic"
    return None


@dataclass(slots=True)
class _FnStats:
    qual_name: str
    is_setup: bool
    assertions: int = 0
    contracts: set[str] = field(default_factory=set)
    mock_targets: set[str] = field(default_factory=set)
    mock_sites: int = 0
    real_signals: int = 0
    uses_server_call: bool = False


def _record_call_in_bucket(
    node: ast.Call,
    callee: str,
    tail: str,
    fake_classes: set[str],
    class_has_mock_server: bool,
    bucket: _FnStats,
) -> None:
    if tail.startswith("assert") or tail in _ASSERT_RAISES_METHODS:
        bucket.assertions += 1
        if tail in _ASSERT_RAISES_METHODS:
            bucket.contracts.update(_extract_raised_exceptions(node) or ["raises:Exception"])
        elif tail in ("assertRegex", "assertNotRegex", "assertLogs"):
            bucket.contracts.add(f"assert:{tail}")
    patch_target = _extract_patch_target(node, callee)
    if patch_target is not None:
        bucket.mock_targets.add(patch_target)
        bucket.mock_sites += 1
    elif tail in _MOCK_CALL_NAMES:
        bucket.mock_targets.add(f"mock:{tail}")
        bucket.mock_sites += 1
    elif tail in fake_classes:
        bucket.mock_targets.add(f"fake:{tail}")
        bucket.mock_sites += 1
    if tail in _REAL_BOUNDARY_NAMES or tail in _REAL_BOUNDARY_METHODS:
        bucket.real_signals += 1
    if class_has_mock_server and "server" in callee.lower():
        bucket.uses_server_call = True


def _classify_suite_boundary(
    mock_sites: int,
    mocked_methods: int,
    method_count: int,
    real_signals: int,
    uses_mock_server: bool,
) -> str:
    if mock_sites == 0 and not uses_mock_server:
        return "real-boundary"
    if (mocked_methods / max(method_count, 1)) >= 0.5:
        return "mock-heavy"
    if (mock_sites >= 3 or uses_mock_server) and real_signals == 0:
        return "mock-heavy"
    return "mixed"


def _scan_test_structure(
    tree: ast.Module,
    known_modules: set[str],
    reexports: dict[tuple[str, str], str],
    helper_map: dict[str, set[str]],
) -> tuple[
    set[str],
    set[str],
    set[str],
    bool,
    dict[int, _FnStats],
    list[_FnStats],
    list[_FnStats],
]:
    """Statement-only pre-scan for imports, local mock classes, and setUp/test_* functions."""
    exercised: set[str] = set()
    imported_syms: set[str] = set()
    fake_classes: set[str] = set()
    uses_mock_server = False
    for stmt in _iter_stmts(tree.body):
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            _collect_import_ref(
                stmt, known_modules, reexports, helper_map, exercised, imported_syms
            )
        elif isinstance(stmt, ast.ClassDef):
            base_names = {_dotted_name(b).rsplit(".", 1)[-1] for b in stmt.bases}
            if base_names & _MOCK_SERVER_BASES:
                fake_classes.add(stmt.name)
                uses_mock_server = True
            elif stmt.name.startswith(_FAKE_CLASS_PREFIXES):
                fake_classes.add(stmt.name)

    bucket_by_id: dict[int, _FnStats] = {}
    setup_buckets: list[_FnStats] = []
    test_buckets: list[_FnStats] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef):
            for item in stmt.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if item.name == "setUp":
                    st = _FnStats(item.name, is_setup=True)
                    bucket_by_id[id(item)] = st
                    setup_buckets.append(st)
                elif item.name.startswith("test_"):
                    st = _FnStats(f"{stmt.name}.{item.name}", is_setup=False)
                    bucket_by_id[id(item)] = st
                    test_buckets.append(st)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name.startswith(
            "test_"
        ):
            st = _FnStats(stmt.name, is_setup=False)
            bucket_by_id[id(stmt)] = st
            test_buckets.append(st)
    return (
        exercised,
        imported_syms,
        fake_classes,
        uses_mock_server,
        bucket_by_id,
        setup_buckets,
        test_buckets,
    )


_AST_BASE = ast.AST
_LEAF_AST_TYPES = (
    ast.Name,
    ast.expr_context,
    ast.operator,
    ast.unaryop,
    ast.cmpop,
    ast.boolop,
    ast.Import,
    ast.ImportFrom,
    ast.Pass,
    ast.Break,
    ast.Continue,
)


def _walk_test_single_pass(
    tree: ast.Module,
    known_modules: set[str],
    path_to_mod: dict[str, str],
    fake_classes: set[str],
    uses_mock_server: bool,
    bucket_by_id: dict[int, _FnStats],
    exercised: set[str],
) -> int:
    """Single-pass iterative AST walk collecting string refs, assertions, mocks, real signals."""
    module_real_signals = 0
    stack: list[tuple[ast.AST, _FnStats | None]] = [(tree, None)]
    pop = stack.pop
    append = stack.append
    get_bucket = bucket_by_id.get
    real_names = _REAL_BOUNDARY_NAMES
    while stack:
        node, bucket = pop()
        if isinstance(node, ast.Constant):
            val = node.value
            if isinstance(val, str) and len(val) >= 5:
                cleaned = val.strip().replace("\\", "/")
                if cleaned in known_modules:
                    exercised.add(cleaned)
                elif cleaned.endswith(".py") and cleaned in path_to_mod:
                    exercised.add(path_to_mod[cleaned])
            continue
        if isinstance(node, _LEAF_AST_TYPES):
            continue
        if isinstance(node, ast.Assert):
            if bucket is not None:
                bucket.assertions += 1
                bucket.contracts.add("assert:stmt")
        elif isinstance(node, ast.Call):
            callee = _dotted_name(node.func)
            tail = callee.rsplit(".", 1)[-1] if callee else ""
            if tail in real_names:
                module_real_signals += 1
            if bucket is not None:
                _record_call_in_bucket(node, callee, tail, fake_classes, uses_mock_server, bucket)
        next_bucket = get_bucket(id(node), bucket)
        for field_name in node._fields:
            child = getattr(node, field_name, None)
            if isinstance(child, _AST_BASE):
                append((child, next_bucket))
            elif type(child) is list:
                for item in child:
                    if isinstance(item, _AST_BASE):
                        append((item, next_bucket))
    return module_real_signals


def _analyze_test_module(
    file_path: Path,
    rel_path: str,
    known_modules: set[str],
    path_to_mod: dict[str, str],
    reexports: dict[tuple[str, str], str],
    helper_map: dict[str, set[str]],
    digest_out: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    raw = read_bytes(file_path, MAX_BYTES)
    if digest_out is not None:
        digest_out[rel_path] = {
            "path": rel_path,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    tree = ast.parse(raw.decode("utf-8"), filename=rel_path)
    (
        exercised_set,
        imported_syms,
        fake_classes,
        uses_mock_server,
        bucket_by_id,
        setup_buckets,
        test_buckets,
    ) = _scan_test_structure(tree, known_modules, reexports, helper_map)
    module_real_signals = _walk_test_single_pass(
        tree,
        known_modules,
        path_to_mod,
        fake_classes,
        uses_mock_server,
        bucket_by_id,
        exercised_set,
    )
    del tree

    exercised = sorted(exercised_set)
    suite_assertions = 0
    suite_contracts: set[str] = set(imported_syms)
    suite_mock_targets: set[str] = {"mock:http-server"} if uses_mock_server else set()
    suite_mock_sites = 1 if uses_mock_server else 0
    real_signals = module_real_signals
    mocked_methods = 0
    methods: list[dict[str, Any]] = []

    for st in setup_buckets:
        if uses_mock_server and st.mock_sites == 0 and st.uses_server_call:
            st.mock_targets.add("mock:http-server")
            st.mock_sites += 1
        suite_mock_targets.update(st.mock_targets)
        suite_mock_sites += st.mock_sites
        real_signals += st.real_signals

    for st in test_buckets:
        if uses_mock_server and st.mock_sites == 0 and st.uses_server_call:
            st.mock_targets.add("mock:http-server")
            st.mock_sites += 1
        suite_assertions += st.assertions
        suite_contracts.update(st.contracts)
        suite_mock_targets.update(st.mock_targets)
        suite_mock_sites += st.mock_sites
        real_signals += st.real_signals
        if st.mock_sites > 0:
            mocked_methods += 1
        methods.append(
            {
                "name": st.qual_name,
                "assertion_count": st.assertions,
                "mock_site_count": st.mock_sites,
                "boundary_class": "mocked" if st.mock_sites > 0 else "real-boundary",
                "asserts_contract": sorted(st.contracts),
            }
        )

    method_count = len(methods)
    boundary_class = _classify_suite_boundary(
        suite_mock_sites, mocked_methods, method_count, real_signals, uses_mock_server
    )
    return {
        "suite_id": _suite_record_id(rel_path),
        "path": rel_path,
        "test_method_count": method_count,
        "assertion_count": suite_assertions,
        "assertion_density": round(suite_assertions / max(method_count, 1), 2),
        "exercises_modules": exercised,
        "exercised_module_ids": sorted(_module_record_id(m) for m in exercised),
        "asserts_contract": sorted(suite_contracts),
        "mocks_boundary": sorted(suite_mock_targets),
        "mock_site_count": suite_mock_sites,
        "mocked_method_count": mocked_methods,
        "real_boundary_signal_count": real_signals,
        "boundary_class": boundary_class,
        "overlap_cluster_id": None,
        "methods": methods,
    }


def _jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return 0.0 if not union else len(a & b) / len(union)


def cluster_overlapping_suites(
    suites: list[dict[str, Any]],
    *,
    threshold: float = 0.8,
) -> list[dict[str, Any]]:
    """Cluster test suites with Jaccard >= threshold on (exercised_modules, asserts_contract)."""
    if not (0.0 < threshold <= 1.0):
        raise ValueError(f"overlap_threshold must be in (0.0, 1.0], got {threshold!r}")
    features: dict[str, set[str]] = {}
    by_id: dict[str, dict[str, Any]] = {}
    for suite in suites:
        sid = str(suite["suite_id"])
        by_id[sid] = suite
        if not suite["exercises_modules"]:
            continue
        features[sid] = {f"mod:{m}" for m in suite["exercises_modules"]} | {
            f"contract:{c}" for c in suite["asserts_contract"]
        }

    ids = sorted(features)
    adj: dict[str, set[str]] = defaultdict(set)
    pair_scores: dict[tuple[str, str], float] = {}
    for idx, left in enumerate(ids):
        for right in ids[idx + 1 :]:
            score = _jaccard(features[left], features[right])
            if score >= threshold:
                adj[left].add(right)
                adj[right].add(left)
                pair_scores[(left, right)] = score

    visited: set[str] = set()
    clusters: list[dict[str, Any]] = []
    for sid in ids:
        if sid in visited or sid not in adj:
            continue
        comp: list[str] = []
        stack = [sid]
        visited.add(sid)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nxt in sorted(adj[cur]):
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append(nxt)
        comp.sort()
        scores = [
            pair_scores[(a, b)]
            for i, a in enumerate(comp)
            for b in comp[i + 1 :]
            if (a, b) in pair_scores
        ]
        shared_mods = sorted(set.intersection(*(set(by_id[m]["exercises_modules"]) for m in comp)))
        shared_contracts = sorted(
            set.intersection(*(set(by_id[m]["asserts_contract"]) for m in comp))
        )
        cluster_id = slugify_id("test-overlap", comp[0])
        for member in comp:
            by_id[member]["overlap_cluster_id"] = cluster_id
        clusters.append(
            {
                "cluster_id": cluster_id,
                "members": comp,
                "min_jaccard": round(min(scores), 4),
                "max_jaccard": round(max(scores), 4),
                "shared_modules": shared_mods,
                "shared_contracts": shared_contracts,
            }
        )
    clusters.sort(key=lambda item: (-len(item["members"]), item["cluster_id"]))
    return clusters


def _transitive_closure(seeds: set[str], prod_modules: dict[str, dict[str, Any]]) -> set[str]:
    reached = set(seeds)
    stack = list(seeds)
    while stack:
        cur = stack.pop()
        for dep in prod_modules.get(cur, {}).get("internal_imports", ()):
            if dep not in reached:
                reached.add(dep)
                stack.append(dep)
    return reached


def identify_verification_unknowns(
    prod_modules: dict[str, dict[str, Any]],
    suites: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Identify non-empty production modules with 0 suites or mock-heavy-only coverage."""
    exercising: dict[str, list[str]] = defaultdict(list)
    real_or_mixed: dict[str, list[str]] = defaultdict(list)
    mock_heavy: dict[str, list[str]] = defaultdict(list)

    for suite in suites:
        sid = str(suite["suite_id"])
        bclass = str(suite["boundary_class"])
        for mod_name in suite["exercises_modules"]:
            exercising[mod_name].append(sid)
            if bclass == "mock-heavy":
                mock_heavy[mod_name].append(sid)
            else:
                real_or_mixed[mod_name].append(sid)

    transitively_reached = _transitive_closure(set(exercising), prod_modules)
    unknowns: list[dict[str, Any]] = []
    for mod_name, info in sorted(prod_modules.items()):
        if not info.get("has_executable_code", True):
            continue
        ex_list = sorted(exercising.get(mod_name, ()))
        rm_list = sorted(real_or_mixed.get(mod_name, ()))
        mh_list = sorted(mock_heavy.get(mod_name, ()))
        if not ex_list:
            reason = "untested"
        elif not rm_list:
            reason = "mock-only"
        else:
            continue
        unknowns.append(
            {
                "unknown_id": _unknown_record_id(mod_name),
                "module": mod_name,
                "module_id": info["module_id"],
                "path": info["path"],
                "lines": info["lines"],
                "is_facade": info["is_facade"],
                "reason": reason,
                "transitive_only": reason == "untested" and mod_name in transitively_reached,
                "exercising_suites": ex_list,
                "mock_heavy_suites": mh_list,
            }
        )
    unknowns.sort(
        key=lambda item: (
            0 if (item["reason"] == "untested" and not item["transitive_only"]) else 1,
            0 if item["reason"] == "mock-only" else 1,
            -item["lines"],
            item["module"],
        )
    )
    return unknowns


def _discover_test_helper_imports(
    test_py_files: list[tuple[Path, str]],
    known_modules: set[str],
    path_to_mod: dict[str, str],
    reexports: dict[tuple[str, str], str],
) -> dict[str, set[str]]:
    """Collect 1-hop production imports from non-test helper modules under tests_dir."""
    helpers: dict[str, set[str]] = {}
    for resolved, rel_posix in test_py_files:
        if resolved.name == "__init__.py" or resolved.name.startswith("test_"):
            continue
        dotted = rel_posix.removesuffix(".py").replace("/", ".")
        tree = ast.parse(read_bytes(resolved, MAX_BYTES).decode("utf-8"), filename=rel_posix)
        mods, _ = _resolve_prod_refs(tree, known_modules, path_to_mod, reexports, {})
        del tree
        if mods:
            helpers[dotted] = mods
    return helpers


def analyze_test_value(
    repo_root: Path,
    *,
    package_rel: str = "src/managed_ai_information",
    tests_rel: str = "tests",
    overlap_threshold: float = 0.8,
    precomputed_prod: tuple[dict[str, dict[str, Any]], dict[tuple[str, str], str]] | None = None,
    digest_out: dict[str, dict[str, Any]] | None = None,
    commit: str | None = None,
) -> dict[str, Any]:
    """Analyze test suites, mock boundaries, overlap clusters, and verification unknowns."""
    checked_repo = check_ordinary_ancestors(
        repo_root, expect_dir=True, error_prefix="ordinary repository root required"
    ).resolve(strict=True)
    if precomputed_prod is not None:
        prod_modules, reexports = precomputed_prod
    else:
        prod_modules, reexports = discover_production_modules(checked_repo, package_rel=package_rel)
    safe_tests_rel = relative_path(tests_rel)
    tests_dir = checked_repo / safe_tests_rel
    path_to_mod: dict[str, str] = {}
    for mod_name, info in prod_modules.items():
        full_rel = str(info["path"])
        path_to_mod[full_rel] = mod_name
        if full_rel.startswith("src/"):
            path_to_mod[full_rel.removeprefix("src/")] = mod_name

    suites: list[dict[str, Any]] = []
    if tests_dir.exists() or tests_dir.is_symlink():
        checked_tests = check_ordinary_ancestors(
            tests_dir,
            expect_dir=True,
            error_prefix=f"ordinary tests directory required ({safe_tests_rel})",
        ).resolve(strict=True)
        if not checked_tests.is_relative_to(checked_repo):
            raise ValueError(f"tests_dir escapes repository root: {safe_tests_rel}")
        known_names = set(prod_modules)
        test_py_files = _iter_ordinary_py_files(checked_repo, checked_tests)
        helper_map = _discover_test_helper_imports(
            test_py_files, known_names, path_to_mod, reexports
        )
        for resolved, rel_posix in test_py_files:
            if not resolved.name.startswith("test_"):
                continue
            suites.append(
                _analyze_test_module(
                    resolved,
                    rel_posix,
                    known_names,
                    path_to_mod,
                    reexports,
                    helper_map,
                    digest_out=digest_out,
                )
            )

    overlap_clusters = cluster_overlapping_suites(suites, threshold=overlap_threshold)
    unknowns = identify_verification_unknowns(prod_modules, suites)

    by_boundary = {"real-boundary": 0, "mixed": 0, "mock-heavy": 0}
    total_methods = 0
    total_assertions = 0
    for suite in suites:
        by_boundary[str(suite["boundary_class"])] += 1
        total_methods += int(suite["test_method_count"])
        total_assertions += int(suite["assertion_count"])

    untested_count = sum(1 for u in unknowns if u["reason"] == "untested")
    unreachable_count = sum(
        1 for u in unknowns if u["reason"] == "untested" and not u["transitive_only"]
    )
    mock_only_count = sum(1 for u in unknowns if u["reason"] == "mock-only")
    return {
        "format": TEST_VALUE_REPORT_FORMAT,
        "repo": str(checked_repo),
        "package_dir": relative_path(package_rel),
        "tests_dir": safe_tests_rel,
        "commit": git_head_sha(checked_repo) if commit is None else commit,
        "overlap_threshold": overlap_threshold,
        "summary": {
            "production_module_count": len(prod_modules),
            "test_suite_count": len(suites),
            "test_method_count": total_methods,
            "total_assertions": total_assertions,
            "avg_assertion_density": round(total_assertions / max(total_methods, 1), 2),
            "by_boundary_class": by_boundary,
            "overlap_cluster_count": len(overlap_clusters),
            "overlapping_suite_count": sum(len(c["members"]) for c in overlap_clusters),
            "verification_unknown_count": len(unknowns),
            "untested_module_count": untested_count,
            "strictly_unreachable_module_count": unreachable_count,
            "mock_only_module_count": mock_only_count,
        },
        "suites": suites,
        "overlap_clusters": overlap_clusters,
        "verification_unknowns": unknowns,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AST analyzer for test value, mock boundaries, overlap, and unknowns.",
        allow_abbrev=False,
    )
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--package-dir", default="src/managed_ai_information")
    parser.add_argument("--tests-dir", default="tests")
    parser.add_argument("--overlap-threshold", type=float, default=0.8)
    parser.add_argument("--summary-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = analyze_test_value(
            args.repo,
            package_rel=args.package_dir,
            tests_rel=args.tests_dir,
            overlap_threshold=args.overlap_threshold,
        )
        if args.summary_only:
            report = {
                "format": report["format"],
                "commit": report["commit"],
                "summary": report["summary"],
                "overlap_clusters": report["overlap_clusters"],
                "top_verification_unknowns": report["verification_unknowns"][:25],
            }
        print(dump_json(report))
        return 0
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {"format": TEST_VALUE_REPORT_FORMAT, "status": "error", "error": str(exc)},
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
