#!/usr/bin/env python3
"""Pure-stdlib AST analyzer for semantic test value, mock boundaries, overlap, and unknowns."""

import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
for _entry in (str(SRC_ROOT), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from managed_ai_information.core.ownership import relative_path  # noqa: E402
from managed_ai_information.core.storage import MAX_BYTES, read_bytes  # noqa: E402
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
    results: list[tuple[Path, str]] = []
    stack = [base_dir]
    while stack:
        cur = stack.pop()
        for entry in sorted(cur.iterdir(), key=lambda p: p.name):
            if entry.name == "__pycache__" or entry.name.startswith("."):
                continue
            info = entry.lstat()
            if entry.is_symlink():
                raise ValueError(f"ordinary non-symlink path required: {entry}")
            if entry.is_dir():
                check_ordinary_ancestors(entry, expect_dir=True, error_prefix="ordinary dir")
                stack.append(entry)
                continue
            if not entry.name.endswith(".py"):
                continue
            if not is_ordinary_file_stat(info):
                raise ValueError(f"ordinary Python file required: {entry}")
            resolved = entry.resolve(strict=True)
            if not resolved.is_relative_to(checked_root):
                raise ValueError(f"source file escapes repo root: {entry}")
            results.append((resolved, resolved.relative_to(checked_root).as_posix()))
    results.sort(key=lambda pair: pair[1])
    return results


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


def _extract_module_imports(
    mod_name: str,
    is_pkg_init: bool,
    tree: ast.Module,
    known_modules: set[str],
) -> tuple[set[str], dict[tuple[str, str], str]]:
    """Return (internal_imports, facade_reexports) for one production module."""
    internal: set[str] = set()
    reexports: dict[tuple[str, str], str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in known_modules:
                    internal.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base_mod = (
                _resolve_relative_import(mod_name, is_pkg_init, node.level, node.module)
                if node.level > 0
                else (node.module or "")
            )
            for alias in node.names:
                if alias.name == "*":
                    continue
                exported = alias.asname or alias.name
                candidate = f"{base_mod}.{alias.name}" if base_mod else alias.name
                if candidate in known_modules:
                    internal.add(candidate)
                    reexports[(mod_name, exported)] = candidate
                elif base_mod in known_modules:
                    internal.add(base_mod)
                    reexports[(mod_name, exported)] = base_mod
    internal.discard(mod_name)
    return internal, reexports


def _has_executable_statements(tree: ast.Module) -> bool:
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
    parsed: dict[str, tuple[ast.Module, bool, bool]] = {}

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
            "has_executable_code": _has_executable_statements(tree),
            "internal_imports": [],
        }
        parsed[mod_name] = (tree, is_init, is_facade)

    known_names = set(modules)
    reexports: dict[tuple[str, str], str] = {}
    for mod_name, (tree, is_init, is_facade) in parsed.items():
        internal, mod_reexports = _extract_module_imports(mod_name, is_init, tree, known_names)
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


def _resolve_prod_refs(
    tree: ast.AST,
    known_modules: set[str],
    path_to_mod: dict[str, str],
    reexports: dict[tuple[str, str], str],
    helper_map: dict[str, set[str]],
) -> tuple[set[str], set[str]]:
    """Return (exercised_prod_modules, imported_prod_symbols) from AST imports & CLI strings."""
    found: set[str] = set()
    symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in known_modules:
                    found.add(alias.name)
                if alias.name in helper_map:
                    found.update(helper_map[alias.name])
        elif isinstance(node, ast.ImportFrom) and node.module:
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
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            val = node.value.strip().replace("\\", "/")
            if val in known_modules:
                found.add(val)
            elif val.endswith(".py") and val in path_to_mod:
                found.add(path_to_mod[val])
    return found, symbols


def _collect_local_mock_classes(tree: ast.Module) -> tuple[set[str], bool]:
    fake_classes: set[str] = set()
    uses_mock_server = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        base_names = {_dotted_name(b).split(".")[-1] for b in node.bases}
        if base_names & _MOCK_SERVER_BASES:
            fake_classes.add(node.name)
            uses_mock_server = True
        elif node.name.startswith(_FAKE_CLASS_PREFIXES):
            fake_classes.add(node.name)
    return fake_classes, uses_mock_server


def _extract_raised_exceptions(call: ast.Call) -> list[str]:
    if not call.args:
        return []
    first = call.args[0]
    if isinstance(first, ast.Tuple):
        names = [_dotted_name(elt).split(".")[-1] for elt in first.elts]
        return [f"raises:{n}" for n in names if n]
    name = _dotted_name(first).split(".")[-1]
    return [f"raises:{name}"] if name else []


def _extract_patch_target(call: ast.Call) -> str | None:
    callee = _dotted_name(call.func)
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


def _inspect_call_node(
    node: ast.Call,
    fake_classes: set[str],
    contracts: set[str],
    mock_targets: set[str],
) -> tuple[int, int, int]:
    """Return (assertions_delta, mock_sites_delta, real_signals_delta) for one AST Call."""
    assertions = 0
    mock_sites = 0
    real_signals = 0
    callee = _dotted_name(node.func)
    tail = callee.split(".")[-1] if callee else ""
    if tail.startswith("assert") or tail in _ASSERT_RAISES_METHODS:
        assertions = 1
        if tail in _ASSERT_RAISES_METHODS:
            contracts.update(_extract_raised_exceptions(node) or ["raises:Exception"])
        elif tail in ("assertRegex", "assertNotRegex", "assertLogs"):
            contracts.add(f"assert:{tail}")
    patch_target = _extract_patch_target(node)
    if patch_target is not None:
        mock_targets.add(patch_target)
        mock_sites = 1
    elif tail in _MOCK_CALL_NAMES:
        mock_targets.add(f"mock:{tail}")
        mock_sites = 1
    elif tail in fake_classes:
        mock_targets.add(f"fake:{tail}")
        mock_sites = 1
    if tail in _REAL_BOUNDARY_NAMES or tail in _REAL_BOUNDARY_METHODS:
        real_signals = 1
    return assertions, mock_sites, real_signals


def _inspect_function_body(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    fake_classes: set[str],
    class_has_mock_server: bool,
) -> tuple[int, set[str], set[str], int, int]:
    """Return (assertions, contracts, mock_targets, mock_sites, real_signals) for one test."""
    assertions = 0
    contracts: set[str] = set()
    mock_targets: set[str] = set()
    mock_sites = 0
    real_signals = 0

    uses_server_call = False
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            assertions += 1
            contracts.add("assert:stmt")
        elif isinstance(node, ast.Call):
            da, dm, dr = _inspect_call_node(node, fake_classes, contracts, mock_targets)
            assertions += da
            mock_sites += dm
            real_signals += dr
            if class_has_mock_server and "server" in _dotted_name(node.func).lower():
                uses_server_call = True

    if class_has_mock_server and mock_sites == 0 and uses_server_call:
        mock_targets.add("mock:http-server")
        mock_sites += 1
    return assertions, contracts, mock_targets, mock_sites, real_signals


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


def _iter_test_functions(
    tree: ast.Module,
) -> tuple[
    list[ast.FunctionDef | ast.AsyncFunctionDef],
    list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]],
]:
    setup_fns: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    test_fns: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef):
            for item in stmt.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if item.name == "setUp":
                    setup_fns.append(item)
                elif item.name.startswith("test_"):
                    test_fns.append((f"{stmt.name}.{item.name}", item))
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name.startswith(
            "test_"
        ):
            test_fns.append((stmt.name, stmt))
    return setup_fns, test_fns


def _analyze_test_module(
    file_path: Path,
    rel_path: str,
    known_modules: set[str],
    path_to_mod: dict[str, str],
    reexports: dict[tuple[str, str], str],
    helper_map: dict[str, set[str]],
) -> dict[str, Any]:
    tree = ast.parse(read_bytes(file_path, MAX_BYTES).decode("utf-8"), filename=rel_path)
    exercised_set, imported_syms = _resolve_prod_refs(
        tree, known_modules, path_to_mod, reexports, helper_map
    )
    exercised = sorted(exercised_set)
    fake_classes, uses_mock_server = _collect_local_mock_classes(tree)
    setup_fns, test_fns = _iter_test_functions(tree)

    suite_assertions = 0
    suite_contracts: set[str] = set(imported_syms)
    suite_mock_targets: set[str] = {"mock:http-server"} if uses_mock_server else set()
    suite_mock_sites = 1 if uses_mock_server else 0
    real_signals = 0
    mocked_methods = 0
    methods: list[dict[str, Any]] = []

    for fn in setup_fns:
        _, _, mt, ms, rs = _inspect_function_body(fn, fake_classes, uses_mock_server)
        suite_mock_targets.update(mt)
        suite_mock_sites += ms
        real_signals += rs

    for qual_name, fn in test_fns:
        a_cnt, c_set, mt, ms, rs = _inspect_function_body(fn, fake_classes, uses_mock_server)
        suite_assertions += a_cnt
        suite_contracts.update(c_set)
        suite_mock_targets.update(mt)
        suite_mock_sites += ms
        real_signals += rs
        if ms > 0:
            mocked_methods += 1
        methods.append(
            {
                "name": qual_name,
                "assertion_count": a_cnt,
                "mock_site_count": ms,
                "boundary_class": "mocked" if ms > 0 else "real-boundary",
                "asserts_contract": sorted(c_set),
            }
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _dotted_name(node.func).split(".")[-1] in (
            _REAL_BOUNDARY_NAMES
        ):
            real_signals += 1

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
    checked_repo: Path,
    tests_dir: Path,
    known_modules: set[str],
    path_to_mod: dict[str, str],
    reexports: dict[tuple[str, str], str],
) -> dict[str, set[str]]:
    """Collect 1-hop production imports from non-test helper modules under tests_dir."""
    helpers: dict[str, set[str]] = {}
    for resolved, rel_posix in _iter_ordinary_py_files(checked_repo, tests_dir):
        if resolved.name == "__init__.py" or resolved.name.startswith("test_"):
            continue
        dotted = rel_posix.removesuffix(".py").replace("/", ".")
        tree = ast.parse(read_bytes(resolved, MAX_BYTES).decode("utf-8"), filename=rel_posix)
        mods, _ = _resolve_prod_refs(tree, known_modules, path_to_mod, reexports, {})
        if mods:
            helpers[dotted] = mods
    return helpers


def analyze_test_value(
    repo_root: Path,
    *,
    package_rel: str = "src/managed_ai_information",
    tests_rel: str = "tests",
    overlap_threshold: float = 0.8,
) -> dict[str, Any]:
    """Analyze test suites, mock boundaries, overlap clusters, and verification unknowns."""
    checked_repo = check_ordinary_ancestors(
        repo_root, expect_dir=True, error_prefix="ordinary repository root required"
    ).resolve(strict=True)
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
        helper_map = _discover_test_helper_imports(
            checked_repo, checked_tests, known_names, path_to_mod, reexports
        )
        for resolved, rel_posix in _iter_ordinary_py_files(checked_repo, checked_tests):
            if not resolved.name.startswith("test_"):
                continue
            suites.append(
                _analyze_test_module(
                    resolved, rel_posix, known_names, path_to_mod, reexports, helper_map
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
        "commit": git_head_sha(checked_repo),
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
