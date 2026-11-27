#!/usr/bin/env python3
"""Pure-stdlib AST and import-graph semantic profiler and CI budget gate."""

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
from collections import Counter, deque
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
for _entry in (str(SRC_ROOT), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from managed_ai_information.platform import (  # noqa: E402
    atomic_bytes,
    check_ordinary_ancestors,
    is_ordinary_dir_stat,
    is_ordinary_file_stat,
    strict_json_loads,
)

PROFILE_FORMAT = "managed-ai-semantic-profile/1"
BASELINE_FORMAT = "managed-ai-semantic-baseline/1"

DEFAULT_BUDGETS: dict[str, int] = {
    "max_file_lines": 650,
    "max_cc": 25,
    "max_depth": 5,
    "max_working_set_tokens": 60000,
}

DEFAULT_RELATIONSHIP_BUDGETS: dict[str, int] = {
    "max_module_sccs": 0,
    "max_subpackage_sccs": 0,
    "max_package_sccs": 0,
    "max_cross_package_private_imports": 0,
    "max_non_primitive_nexus": 80,
}

_METRIC_TO_BUDGET_KEY: tuple[tuple[str, str], ...] = (
    ("lines", "max_file_lines"),
    ("max_cc", "max_cc"),
    ("max_depth", "max_depth"),
    ("working_set_tokens", "max_working_set_tokens"),
)

_REL_METRIC_TO_BUDGET_KEY: tuple[tuple[str, str], ...] = (
    ("module_scc_count", "max_module_sccs"),
    ("subpackage_scc_count", "max_subpackage_sccs"),
    ("package_scc_count", "max_package_sccs"),
    ("cross_package_private_imports", "max_cross_package_private_imports"),
    ("max_non_primitive_nexus", "max_non_primitive_nexus"),
)

_PRIMITIVE_SUFFIXES: frozenset[str] = frozenset(
    {
        "platform",
        "core.storage",
        "core.formats",
        "core.ownership",
        "store.contract",
        "store.protocol.identities",
        "store.protocol.inputs",
    }
)

_IGNORED_DIRS = frozenset({"__pycache__", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache"})
_MAX_COCHANGE_COMMIT_FILES = 30


def _read_ordinary_utf8(path: Path, label: str) -> str:
    checked = check_ordinary_ancestors(
        path,
        expect_dir=False,
        error_prefix=f"ordinary non-symlink file required ({label})",
    )
    info = checked.lstat()
    if not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary non-symlink file required ({label}): {checked}")
    return checked.read_text(encoding="utf-8")


def _walk_python_files(root: Path, package_dir: Path) -> list[tuple[str, Path]]:
    collected: list[tuple[str, Path]] = []
    stack: list[Path] = [package_dir]
    while stack:
        current = stack.pop()
        with os.scandir(current) as entries:
            ordered = sorted(entries, key=lambda item: item.name)
        for entry in ordered:
            name = entry.name
            if name in _IGNORED_DIRS or name == ".DS_Store":
                continue
            path = Path(entry.path)
            info = path.lstat()
            if is_ordinary_dir_stat(info):
                stack.append(path)
                continue
            if not is_ordinary_file_stat(info):
                raise ValueError(f"ordinary file or directory required: {path}")
            if path.suffix != ".py":
                continue
            rel = path.relative_to(root).as_posix()
            collected.append((rel, path))
    collected.sort(key=lambda item: item[0])
    return collected


def _module_name_for_path(package_dir: Path, file_path: Path) -> str:
    rel = file_path.relative_to(package_dir.parent).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, round(len(text) / 4))


def _node_cyclomatic_complexity(fn_node: ast.AST) -> int:
    """Compute McCabe cyclomatic complexity for one function without descending into nested defs."""
    complexity = 1
    queue: deque[ast.AST] = deque(ast.iter_child_nodes(fn_node))
    while queue:
        node = queue.popleft()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(
            node, (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler)
        ):
            complexity += 1
        elif isinstance(node, ast.BoolOp):
            complexity += max(0, len(node.values) - 1)
        elif isinstance(node, ast.comprehension):
            complexity += len(node.ifs)
        elif isinstance(node, ast.match_case) and not isinstance(node.pattern, ast.MatchAs):
            complexity += 1
        queue.extend(ast.iter_child_nodes(node))
    return complexity


def _stmt_list_depth(stmts: list[ast.stmt], depth: int = 0) -> int:
    """Compute maximum control-flow nesting depth in a statement list (elif chains share depth)."""
    best = depth
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(stmt, ast.If):
            cur = depth + 1
            best = max(best, cur, _stmt_list_depth(stmt.body, cur))
            orelse = stmt.orelse
            while len(orelse) == 1 and isinstance(orelse[0], ast.If):
                branch = orelse[0]
                best = max(best, _stmt_list_depth(branch.body, cur))
                orelse = branch.orelse
            if orelse:
                best = max(best, _stmt_list_depth(orelse, cur))
        elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
            cur = depth + 1
            best = max(best, cur, _stmt_list_depth(stmt.body, cur))
            orelse = getattr(stmt, "orelse", [])
            if orelse:
                best = max(best, _stmt_list_depth(orelse, cur))
        elif isinstance(stmt, (ast.Try, ast.TryStar)):
            cur = depth + 1
            best = max(best, cur, _stmt_list_depth(stmt.body, cur))
            for handler in stmt.handlers:
                best = max(best, _stmt_list_depth(handler.body, cur))
            if stmt.orelse:
                best = max(best, _stmt_list_depth(stmt.orelse, cur))
            if stmt.finalbody:
                best = max(best, _stmt_list_depth(stmt.finalbody, cur))
        elif isinstance(stmt, ast.Match):
            cur = depth + 1
            best = max(best, cur)
            for case in stmt.cases:
                best = max(best, _stmt_list_depth(case.body, cur))
    return best


def _collect_function_metrics(tree: ast.Module) -> tuple[int, str, int, str, int]:
    """Return (max_cc, max_cc_fn, max_depth, max_depth_fn, function_count)."""
    max_cc = 1
    max_cc_fn = "<module>"
    max_depth = _stmt_list_depth(tree.body)
    max_depth_fn = "<module>"
    function_count = 0

    def walk_scope(body: list[ast.stmt], prefix: str) -> None:
        nonlocal max_cc, max_cc_fn, max_depth, max_depth_fn, function_count
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                qual = f"{prefix}.{stmt.name}" if prefix else stmt.name
                walk_scope(stmt.body, qual)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function_count += 1
                qual = f"{prefix}.{stmt.name}" if prefix else stmt.name
                cc = _node_cyclomatic_complexity(stmt)
                if cc > max_cc:
                    max_cc = cc
                    max_cc_fn = qual
                depth = _stmt_list_depth(stmt.body)
                if depth > max_depth:
                    max_depth = depth
                    max_depth_fn = qual
                walk_scope(stmt.body, qual)

    walk_scope(tree.body, "")
    return max_cc, max_cc_fn, max_depth, max_depth_fn, function_count


def _resolve_relative_base(module_name: str, is_package: bool, level: int) -> str | None:
    parts = module_name.split(".")
    pkg_parts = parts if is_package else parts[:-1]
    trim = level - 1
    if trim > len(pkg_parts):
        return None
    base_parts = pkg_parts[: len(pkg_parts) - trim] if trim else pkg_parts
    return ".".join(base_parts)


def _match_known_module(candidate: str, known_modules: set[str]) -> str | None:
    if candidate in known_modules:
        return candidate
    parts = candidate.split(".")
    while len(parts) > 1:
        parts.pop()
        parent = ".".join(parts)
        if parent in known_modules:
            return parent
    return None


def _is_private_symbol(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _extract_imports_and_privates(
    tree: ast.Module,
    module_name: str,
    is_package: bool,
    known_modules: set[str],
) -> tuple[list[str], list[tuple[str, str]]]:
    """Return (sorted internal target modules, sorted (target_module, _private_symbol) hits)."""
    targets: set[str] = set()
    privates: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                matched = _match_known_module(alias.name, known_modules)
                if matched is not None and matched != module_name:
                    targets.add(matched)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                base = _resolve_relative_base(module_name, is_package, node.level)
                if base is None:
                    continue
                mod_base = f"{base}.{node.module}" if node.module else base
            else:
                if not node.module:
                    continue
                mod_base = node.module

            matched_any = False
            for alias in node.names:
                if alias.name == "*":
                    continue
                sub_cand = f"{mod_base}.{alias.name}"
                if sub_cand in known_modules and sub_cand != module_name:
                    targets.add(sub_cand)
                    matched_any = True
                elif _is_private_symbol(alias.name):
                    owner = _match_known_module(mod_base, known_modules)
                    if owner is not None and owner != module_name:
                        privates.add((owner, alias.name))
            if not matched_any:
                matched = _match_known_module(mod_base, known_modules)
                if matched is not None and matched != module_name:
                    targets.add(matched)
    return sorted(targets), sorted(privates)


def _working_set_closure(
    start_module: str,
    adjacency: dict[str, list[str]],
    token_map: dict[str, int],
) -> tuple[int, int]:
    """Return (closure_module_count, closure_token_sum) for start_module."""
    visited: set[str] = {start_module}
    queue: deque[str] = deque([start_module])
    while queue:
        cur = queue.popleft()
        for nxt in adjacency.get(cur, ()):
            if nxt not in visited:
                visited.add(nxt)
                queue.append(nxt)
    return len(visited), sum(token_map[m] for m in visited)


def _reachable_excluding_self(start: str, adjacency: dict[str, list[str]]) -> int:
    visited: set[str] = {start}
    queue: deque[str] = deque([start])
    while queue:
        cur = queue.popleft()
        for nxt in adjacency.get(cur, ()):
            if nxt not in visited:
                visited.add(nxt)
                queue.append(nxt)
    return len(visited) - 1


def _tarjan_sccs(nodes: list[str], adjacency: dict[str, list[str]]) -> list[list[str]]:
    """Compute Tarjan strongly connected components with |SCC| >= 2 (deterministic order)."""
    index_counter = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal index_counter
        indices[node] = index_counter
        lowlink[node] = index_counter
        index_counter += 1
        stack.append(node)
        on_stack.add(node)

        for nxt in adjacency.get(node, ()):
            if nxt not in indices:
                strongconnect(nxt)
                lowlink[node] = min(lowlink[node], lowlink[nxt])
            elif nxt in on_stack:
                lowlink[node] = min(lowlink[node], indices[nxt])

        if lowlink[node] == indices[node]:
            comp: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                comp.append(member)
                if member == node:
                    break
            if len(comp) >= 2:
                components.append(sorted(comp))

    for node in sorted(nodes):
        if node not in indices:
            strongconnect(node)
    components.sort(key=lambda c: (-len(c), c[0]))
    return components


def _scope_group(module_name: str, depth: int) -> str:
    parts = module_name.split(".")
    if len(parts) <= 1:
        return parts[0]
    rel = parts[1:]
    return ".".join(rel[:depth])


def _is_primitive_module(module_name: str) -> bool:
    parts = module_name.split(".")
    if len(parts) <= 1:
        return True
    suffix = ".".join(parts[1:])
    return suffix in _PRIMITIVE_SUFFIXES or suffix.startswith("platform.")


def _compute_sccs_all_levels(
    adjacency: dict[str, list[str]],
    token_map: dict[str, int],
) -> dict[str, list[dict[str, Any]]]:
    """Compute module, subpackage, and top-level package Tarjan SCCs with locked tokens."""
    all_mods = sorted(adjacency)
    mod_sccs = [
        {"members": comp, "size": len(comp), "tokens_locked": sum(token_map[m] for m in comp)}
        for comp in _tarjan_sccs(all_mods, adjacency)
    ]

    result: dict[str, list[dict[str, Any]]] = {"module": mod_sccs}
    for level_name, depth in (("subpackage", 2), ("package", 1)):
        group_tokens: Counter[str] = Counter()
        group_edges: dict[str, set[str]] = {}
        for mod in all_mods:
            src_g = _scope_group(mod, depth)
            group_tokens[src_g] += token_map[mod]
            group_edges.setdefault(src_g, set())
            for target in adjacency[mod]:
                dst_g = _scope_group(target, depth)
                if dst_g != src_g:
                    group_edges[src_g].add(dst_g)
        g_adj = {g: sorted(dsts) for g, dsts in group_edges.items()}
        comps = _tarjan_sccs(sorted(group_edges), g_adj)
        result[level_name] = [
            {
                "members": comp,
                "size": len(comp),
                "tokens_locked": sum(group_tokens[g] for g in comp),
            }
            for comp in comps
        ]
    return result


def _compute_bypasses_and_privates(
    adjacency: dict[str, list[str]],
    raw_privates: dict[str, list[tuple[str, str]]],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, int], dict[str, int]]:
    """Compute shortcut triangles (layer/facade bypasses) and cross-boundary _private imports."""
    adj_sets = {m: set(targets) for m, targets in adjacency.items()}
    bypass_pairs: set[tuple[str, str]] = set()
    cross_sub_pairs: set[tuple[str, str]] = set()
    bypass_out: Counter[str] = Counter()
    bypass_in: Counter[str] = Counter()

    for caller, direct in adj_sets.items():
        for mid in direct:
            for target in adj_sets.get(mid, ()):
                if target in direct and target != caller and not _is_primitive_module(target):
                    pair = (caller, target)
                    if pair not in bypass_pairs:
                        bypass_pairs.add(pair)
                        bypass_out[caller] += 1
                        bypass_in[target] += 1
                        if _scope_group(caller, 2) != _scope_group(target, 2):
                            cross_sub_pairs.add(pair)

    private_rows: list[dict[str, Any]] = []
    for caller in sorted(raw_privates):
        c_pkg = _scope_group(caller, 1)
        c_sub = _scope_group(caller, 2)
        for target, symbol in raw_privates[caller]:
            t_pkg = _scope_group(target, 1)
            t_sub = _scope_group(target, 2)
            if c_pkg != t_pkg:
                scope = "cross_package"
            elif c_sub != t_sub:
                scope = "cross_subpackage"
            else:
                continue
            private_rows.append(
                {"caller": caller, "target": target, "symbol": symbol, "scope": scope}
            )

    bypass_summary = {
        "shortcut_pair_count": len(bypass_pairs),
        "cross_subpackage_shortcut_count": len(cross_sub_pairs),
        "top_bypassed_targets": [
            {"module": mod, "bypass_in": count} for mod, count in bypass_in.most_common(10)
        ],
        "top_bypassing_callers": [
            {"module": mod, "bypass_out": count} for mod, count in bypass_out.most_common(10)
        ],
    }
    return bypass_summary, private_rows, dict(bypass_out), dict(bypass_in)


def _compute_phantom_cochange(
    root: Path,
    package_rel: str,
    path_to_module: dict[str, str],
    adjacency: dict[str, list[str]],
    *,
    window: int,
    min_cochange: int,
) -> dict[str, Any]:
    """Find file pairs co-changing in git history with zero direct import edge."""
    git_bin = shutil.which("git")
    if git_bin is None:
        return {"available": False, "reason": "git binary not found", "pairs": []}
    if not (root / ".git").exists():
        return {"available": False, "reason": "not a git checkout", "pairs": []}

    proc = subprocess.run(  # noqa: S603
        [
            git_bin,
            "-C",
            str(root),
            "log",
            f"-n{window}",
            "--name-only",
            "--pretty=format:---COMMIT---",
            "--",
            package_rel,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        return {"available": False, "reason": proc.stderr.strip() or "git log failed", "pairs": []}

    pair_counts: Counter[tuple[str, str]] = Counter()
    commits_scanned = 0
    current_files: list[str] = []

    def flush_commit(files: list[str]) -> None:
        nonlocal commits_scanned
        unique = sorted({f for f in files if f in path_to_module})
        if len(unique) < 2 or len(unique) > _MAX_COCHANGE_COMMIT_FILES:
            return
        commits_scanned += 1
        for i, first in enumerate(unique):
            for second in unique[i + 1 :]:
                pair_counts[(first, second)] += 1

    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if line == "---COMMIT---":
            flush_commit(current_files)
            current_files = []
        elif line:
            current_files.append(line)
    flush_commit(current_files)

    adj_sets = {m: set(targets) for m, targets in adjacency.items()}
    phantom_pairs: list[dict[str, Any]] = []
    for (first, second), count in sorted(
        pair_counts.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])
    ):
        if count < min_cochange:
            continue
        mod_a = path_to_module[first]
        mod_b = path_to_module[second]
        if mod_b in adj_sets.get(mod_a, ()) or mod_a in adj_sets.get(mod_b, ()):
            continue
        phantom_pairs.append(
            {
                "file_a": first,
                "file_b": second,
                "module_a": mod_a,
                "module_b": mod_b,
                "cochange_count": count,
            }
        )

    return {
        "available": True,
        "window": window,
        "min_cochange": min_cochange,
        "commits_scanned": commits_scanned,
        "pair_count": len(phantom_pairs),
        "pairs": phantom_pairs[:25],
    }


def _build_profile_report(
    checked_root: Path,
    package_rel: str,
    modules: list[dict[str, Any]],
    relationships: dict[str, Any],
    top_n: int,
) -> dict[str, Any]:
    modules.sort(key=lambda item: item["path"])
    top_hotspots = sorted(
        modules,
        key=lambda m: (-m["working_set_tokens"], -m["max_cc"], -m["lines"], m["path"]),
    )[:top_n]
    coupling_hubs = sorted(
        modules,
        key=lambda m: (-(m["fan_in"] + m["fan_out"]), -m["fan_in"], m["path"]),
    )[:top_n]
    non_prim = [m for m in modules if not m["is_primitive"]]
    max_non_prim_nexus = max((m["nexus_score"] for m in non_prim), default=0)

    rel_summary = relationships["summary"]
    summary = {
        "module_count": len(modules),
        "total_lines": sum(m["lines"] for m in modules),
        "total_tokens": sum(m["tokens"] for m in modules),
        "max_file_lines": max(m["lines"] for m in modules),
        "max_cc": max(m["max_cc"] for m in modules),
        "max_depth": max(m["max_depth"] for m in modules),
        "max_working_set_tokens": max(m["working_set_tokens"] for m in modules),
        "module_scc_count": rel_summary["module_scc_count"],
        "subpackage_scc_count": rel_summary["subpackage_scc_count"],
        "package_scc_count": rel_summary["package_scc_count"],
        "cross_package_private_imports": rel_summary["cross_package_private_imports"],
        "max_non_primitive_nexus": max_non_prim_nexus,
    }

    return {
        "format": PROFILE_FORMAT,
        "root": checked_root.as_posix(),
        "package_dir": package_rel,
        "summary": summary,
        "relationships": relationships,
        "top_hotspots": [
            {
                "path": m["path"],
                "lines": m["lines"],
                "tokens": m["tokens"],
                "working_set_tokens": m["working_set_tokens"],
                "working_set_modules": m["working_set_modules"],
                "max_cc": m["max_cc"],
                "max_cc_function": m["max_cc_function"],
                "max_depth": m["max_depth"],
                "fan_in": m["fan_in"],
                "fan_out": m["fan_out"],
                "nexus_score": m["nexus_score"],
                "blast_radius": m["blast_radius"],
            }
            for m in top_hotspots
        ],
        "coupling_hubs": [
            {
                "path": m["path"],
                "fan_in": m["fan_in"],
                "fan_out": m["fan_out"],
                "nexus_score": m["nexus_score"],
                "blast_radius": m["blast_radius"],
                "lines": m["lines"],
                "working_set_tokens": m["working_set_tokens"],
            }
            for m in coupling_hubs
        ],
        "modules": modules,
    }


def _parse_package_modules(
    package_dir: Path,
    files: list[tuple[str, Path]],
) -> tuple[list[dict[str, Any]], set[str], dict[str, tuple[ast.Module, bool]]]:
    parsed_rows: list[dict[str, Any]] = []
    known_modules: set[str] = set()
    trees: dict[str, tuple[ast.Module, bool]] = {}

    for rel, path in files:
        text = _read_ordinary_utf8(path, rel)
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError as exc:
            raise ValueError(f"syntax error in {rel}:{exc.lineno}: {exc.msg}") from exc

        mod_name = _module_name_for_path(package_dir, path)
        is_pkg = path.name == "__init__.py"
        known_modules.add(mod_name)
        trees[mod_name] = (tree, is_pkg)

        max_cc, max_cc_fn, max_depth, max_depth_fn, fn_count = _collect_function_metrics(tree)
        parsed_rows.append(
            {
                "path": rel,
                "module": mod_name,
                "lines": len(text.splitlines()),
                "tokens": _estimate_tokens(text),
                "functions": fn_count,
                "max_cc": max_cc,
                "max_cc_function": max_cc_fn,
                "max_depth": max_depth,
                "max_depth_function": max_depth_fn,
            }
        )
    return parsed_rows, known_modules, trees


def _assemble_relationships(
    checked_root: Path,
    package_rel: str,
    modules: list[dict[str, Any]],
    adjacency: dict[str, list[str]],
    token_map: dict[str, int],
    raw_privates: dict[str, list[tuple[str, str]]],
    *,
    top_n: int,
    git_cochange: bool,
    cochange_window: int,
    cochange_min: int,
) -> tuple[dict[str, Any], dict[str, int], dict[str, int]]:
    sccs = _compute_sccs_all_levels(adjacency, token_map)
    bypasses, private_rows, bypass_out, bypass_in = _compute_bypasses_and_privates(
        adjacency, raw_privates
    )
    non_prim = [m for m in modules if not m["is_primitive"]]
    nexus_hubs = sorted(
        non_prim,
        key=lambda m: (-m["nexus_score"], -m["blast_radius"], m["path"]),
    )[:top_n]
    cross_pkg_priv = sum(1 for row in private_rows if row["scope"] == "cross_package")
    cross_sub_priv = sum(1 for row in private_rows if row["scope"] == "cross_subpackage")

    path_to_mod = {m["path"]: m["module"] for m in modules}
    phantom = (
        _compute_phantom_cochange(
            checked_root,
            package_rel,
            path_to_mod,
            adjacency,
            window=cochange_window,
            min_cochange=cochange_min,
        )
        if git_cochange
        else {"available": False, "reason": "disabled (pass --git-cochange)", "pairs": []}
    )

    rel_summary = {
        "module_scc_count": len(sccs["module"]),
        "module_scc_tokens_locked": sum(c["tokens_locked"] for c in sccs["module"]),
        "subpackage_scc_count": len(sccs["subpackage"]),
        "subpackage_scc_tokens_locked": sum(c["tokens_locked"] for c in sccs["subpackage"]),
        "package_scc_count": len(sccs["package"]),
        "package_scc_tokens_locked": sum(c["tokens_locked"] for c in sccs["package"]),
        "shortcut_pair_count": bypasses["shortcut_pair_count"],
        "cross_subpackage_shortcut_count": bypasses["cross_subpackage_shortcut_count"],
        "cross_package_private_imports": cross_pkg_priv,
        "cross_subpackage_private_imports": cross_sub_priv,
        "max_non_primitive_nexus": max((m["nexus_score"] for m in non_prim), default=0),
    }

    relationships: dict[str, Any] = {
        "summary": rel_summary,
        "sccs": sccs,
        "bypasses": bypasses,
        "private_imports": private_rows,
        "nexus_hubs": [
            {
                "path": m["path"],
                "module": m["module"],
                "fan_in": m["fan_in"],
                "fan_out": m["fan_out"],
                "nexus_score": m["nexus_score"],
                "ancestors": m["ancestors"],
                "descendants": m["descendants"],
                "blast_radius": m["blast_radius"],
                "lines": m["lines"],
            }
            for m in nexus_hubs
        ],
        "phantom_cochange": phantom,
    }
    return relationships, bypass_out, bypass_in


def profile_repository(
    root: Path,
    *,
    package_rel: str = "src/managed_ai_information",
    top_n: int = 15,
    git_cochange: bool = False,
    cochange_window: int = 100,
    cochange_min: int = 3,
) -> dict[str, Any]:
    """Profile all Python modules under package_rel and compute import-graph bottlenecks."""
    if top_n < 1:
        raise ValueError("--top must be >= 1")
    if cochange_window < 1 or cochange_min < 2:
        raise ValueError("--cochange-window must be >= 1 and --cochange-min must be >= 2")
    checked_root = check_ordinary_ancestors(
        root,
        expect_dir=True,
        error_prefix="ordinary repository root required",
    ).resolve(strict=True)
    package_dir = check_ordinary_ancestors(
        checked_root / package_rel,
        expect_dir=True,
        error_prefix=f"ordinary package directory required ({package_rel})",
    ).resolve(strict=True)
    if not package_dir.is_relative_to(checked_root):
        raise ValueError(f"package directory must stay inside repository root: {package_dir}")

    files = _walk_python_files(checked_root, package_dir)
    if not files:
        raise ValueError(f"no Python modules found under {package_dir}")

    parsed_rows, known_modules, trees = _parse_package_modules(package_dir, files)
    adjacency: dict[str, list[str]] = {}
    reverse_adj: dict[str, list[str]] = {m: [] for m in known_modules}
    raw_privates: dict[str, list[tuple[str, str]]] = {}
    fan_in_counts: dict[str, int] = dict.fromkeys(known_modules, 0)
    token_map: dict[str, int] = {row["module"]: row["tokens"] for row in parsed_rows}

    for mod_name in sorted(known_modules):
        tree, is_pkg = trees[mod_name]
        imports, privs = _extract_imports_and_privates(tree, mod_name, is_pkg, known_modules)
        adjacency[mod_name] = imports
        if privs:
            raw_privates[mod_name] = privs
        for target in imports:
            fan_in_counts[target] += 1
            reverse_adj[target].append(mod_name)

    modules: list[dict[str, Any]] = []
    for row in parsed_rows:
        mod_name = row["module"]
        imports = adjacency[mod_name]
        fan_in = fan_in_counts[mod_name]
        fan_out = len(imports)
        ws_modules, ws_tokens = _working_set_closure(mod_name, adjacency, token_map)
        ancestors = _reachable_excluding_self(mod_name, reverse_adj)
        descendants = _reachable_excluding_self(mod_name, adjacency)
        modules.append(
            {
                **row,
                "fan_in": fan_in,
                "fan_out": fan_out,
                "nexus_score": fan_in * fan_out,
                "ancestors": ancestors,
                "descendants": descendants,
                "blast_radius": ancestors * descendants,
                "is_primitive": _is_primitive_module(mod_name),
                "working_set_modules": ws_modules,
                "working_set_tokens": ws_tokens,
                "internal_imports": imports,
            }
        )

    relationships, bypass_out, bypass_in = _assemble_relationships(
        checked_root,
        package_rel,
        modules,
        adjacency,
        token_map,
        raw_privates,
        top_n=top_n,
        git_cochange=git_cochange,
        cochange_window=cochange_window,
        cochange_min=cochange_min,
    )
    for mod in modules:
        mod["bypass_out"] = bypass_out.get(mod["module"], 0)
        mod["bypass_in"] = bypass_in.get(mod["module"], 0)

    return _build_profile_report(checked_root, package_rel, modules, relationships, top_n)


def _validate_positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _validate_non_negative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def build_baseline(
    profile: dict[str, Any],
    *,
    budgets: dict[str, int] | None = None,
    relationship_budgets: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build a deterministic semantic-baseline.json capturing ceilings and explicit allowances."""
    resolved_budgets = dict(DEFAULT_BUDGETS if budgets is None else budgets)
    for key in DEFAULT_BUDGETS:
        _validate_positive_int(resolved_budgets.get(key), f"budgets.{key}")

    rel_targets = dict(
        DEFAULT_RELATIONSHIP_BUDGETS if relationship_budgets is None else relationship_budgets
    )
    rel_summary = profile.get("relationships", {}).get("summary", {})
    resolved_rel_budgets: dict[str, int] = {}
    for metric_key, budget_key in _REL_METRIC_TO_BUDGET_KEY:
        target = _validate_non_negative_int(
            rel_targets.get(budget_key), f"relationship_budgets.{budget_key}"
        )
        actual = int(rel_summary.get(metric_key, 0))
        resolved_rel_budgets[budget_key] = max(target, actual)

    allowances: dict[str, dict[str, int]] = {}
    for mod in profile["modules"]:
        mod_allow: dict[str, int] = {}
        for metric_key, budget_key in _METRIC_TO_BUDGET_KEY:
            actual = mod[metric_key]
            if actual > resolved_budgets[budget_key]:
                mod_allow[metric_key] = actual
        if mod_allow:
            allowances[mod["path"]] = mod_allow

    return {
        "format": BASELINE_FORMAT,
        "package_dir": profile["package_dir"],
        "budgets": resolved_budgets,
        "relationship_budgets": resolved_rel_budgets,
        "allowances": dict(sorted(allowances.items())),
        "summary": dict(profile["summary"]),
    }


def load_baseline(path: Path) -> dict[str, Any]:
    """Load and strictly validate a semantic-baseline.json file."""
    raw = _read_ordinary_utf8(path, "baseline")
    data = strict_json_loads(raw, label="semantic baseline")
    if not isinstance(data, dict) or data.get("format") != BASELINE_FORMAT:
        raise ValueError(f"baseline must be a JSON object with format {BASELINE_FORMAT!r}")

    budgets_raw = data.get("budgets")
    if not isinstance(budgets_raw, dict) or set(budgets_raw) != set(DEFAULT_BUDGETS):
        raise ValueError(f"baseline budgets must define exact keys: {sorted(DEFAULT_BUDGETS)}")
    budgets: dict[str, int] = {
        key: _validate_positive_int(budgets_raw[key], f"budgets.{key}") for key in DEFAULT_BUDGETS
    }

    rel_raw = data.get("relationship_budgets")
    rel_budgets: dict[str, int] | None = None
    if rel_raw is not None:
        if not isinstance(rel_raw, dict) or set(rel_raw) != set(DEFAULT_RELATIONSHIP_BUDGETS):
            raise ValueError(
                "baseline relationship_budgets must define exact keys: "
                f"{sorted(DEFAULT_RELATIONSHIP_BUDGETS)}"
            )
        rel_budgets = {
            key: _validate_non_negative_int(rel_raw[key], f"relationship_budgets.{key}")
            for key in DEFAULT_RELATIONSHIP_BUDGETS
        }

    allow_raw = data.get("allowances")
    if not isinstance(allow_raw, dict):
        raise ValueError("baseline allowances must be a JSON object")
    valid_metric_keys = {metric_key for metric_key, _ in _METRIC_TO_BUDGET_KEY}
    allowances: dict[str, dict[str, int]] = {}
    for mod_path, entry in allow_raw.items():
        if not isinstance(mod_path, str) or not mod_path.strip():
            raise ValueError("baseline allowance keys must be non-empty relative paths")
        if not isinstance(entry, dict) or not entry:
            raise ValueError(f"baseline allowance for {mod_path} must be a non-empty object")
        unknown = set(entry) - valid_metric_keys
        if unknown:
            raise ValueError(f"unknown allowance metrics for {mod_path}: {sorted(unknown)}")
        allowances[mod_path] = {
            metric: _validate_positive_int(val, f"allowances[{mod_path!r}].{metric}")
            for metric, val in entry.items()
        }

    package_dir = data.get("package_dir", "src/managed_ai_information")
    if not isinstance(package_dir, str) or not package_dir.strip():
        raise ValueError("baseline package_dir must be a non-empty string")

    return {
        "format": BASELINE_FORMAT,
        "package_dir": package_dir,
        "budgets": budgets,
        "relationship_budgets": rel_budgets,
        "allowances": allowances,
    }


def check_budget(profile: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Compare a computed profile against repo-wide budgets, allowances, and relationship caps."""
    budgets: dict[str, int] = baseline["budgets"]
    allowances: dict[str, dict[str, int]] = baseline["allowances"]
    rel_budgets: dict[str, int] | None = baseline.get("relationship_budgets")
    violations: list[dict[str, Any]] = []
    tightenable: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    for mod in profile["modules"]:
        mod_path: str = mod["path"]
        seen_paths.add(mod_path)
        mod_allow = allowances.get(mod_path, {})
        for metric_key, budget_key in _METRIC_TO_BUDGET_KEY:
            actual: int = mod[metric_key]
            base_ceiling = budgets[budget_key]
            allowed = mod_allow.get(metric_key)
            if allowed is not None and allowed > base_ceiling:
                ceiling = allowed
                source = "allowance"
            else:
                ceiling = base_ceiling
                source = "budget"

            if actual > ceiling:
                violations.append(
                    {
                        "path": mod_path,
                        "metric": metric_key,
                        "budget_key": budget_key,
                        "actual": actual,
                        "ceiling": ceiling,
                        "source": source,
                        "function": (
                            mod["max_cc_function"]
                            if metric_key == "max_cc"
                            else mod["max_depth_function"]
                            if metric_key == "max_depth"
                            else None
                        ),
                    }
                )
            elif allowed is not None and actual < allowed:
                tightenable.append(
                    {
                        "path": mod_path,
                        "metric": metric_key,
                        "actual": actual,
                        "allowance": allowed,
                    }
                )

    if rel_budgets is not None:
        rel_summary = profile.get("relationships", {}).get("summary", {})
        for metric_key, budget_key in _REL_METRIC_TO_BUDGET_KEY:
            actual = int(rel_summary.get(metric_key, 0))
            ceiling = rel_budgets[budget_key]
            if actual > ceiling:
                violations.append(
                    {
                        "path": "<relationships>",
                        "metric": metric_key,
                        "budget_key": budget_key,
                        "actual": actual,
                        "ceiling": ceiling,
                        "source": "relationship_budget",
                        "function": None,
                    }
                )
            elif actual < ceiling and ceiling > DEFAULT_RELATIONSHIP_BUDGETS[budget_key]:
                tightenable.append(
                    {
                        "path": "<relationships>",
                        "metric": metric_key,
                        "actual": actual,
                        "allowance": ceiling,
                    }
                )

    stale_allowances = sorted(set(allowances) - seen_paths)
    return {
        "format": PROFILE_FORMAT,
        "status": "passed" if not violations else "failed",
        "violation_count": len(violations),
        "violations": violations,
        "tightenable_count": len(tightenable),
        "tightenable": tightenable,
        "stale_allowances": stale_allowances,
        "summary": profile["summary"],
        "budgets": budgets,
        "relationship_budgets": rel_budgets,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compute AST and import-graph semantic complexity metrics, relationship "
            "bottlenecks, and enforce CI budget baselines."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root directory to profile (default: this checkout).",
    )
    parser.add_argument(
        "--package-dir",
        default="src/managed_ai_information",
        help=(
            "Repository-relative Python package directory to scan "
            "(default: src/managed_ai_information)."
        ),
    )
    parser.add_argument(
        "--top",
        type=int,
        default=15,
        help="Number of top hotspots and coupling hubs to include (default: 15).",
    )
    parser.add_argument(
        "--git-cochange",
        action="store_true",
        help="Include phantom git co-change coupling analysis (pairs with 0 direct import edge).",
    )
    parser.add_argument("--cochange-window", type=int, default=100)
    parser.add_argument("--cochange-min", type=int, default=3)
    parser.add_argument(
        "--write-baseline",
        type=Path,
        default=None,
        help="Atomically write a semantic-baseline.json file to this path.",
    )
    parser.add_argument(
        "--check-budget",
        action="store_true",
        help="Check profiled modules against --baseline and exit 1 on any regression.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Path to semantic-baseline.json (required with --check-budget).",
    )
    parser.add_argument("--budget-lines", type=int, default=DEFAULT_BUDGETS["max_file_lines"])
    parser.add_argument("--budget-cc", type=int, default=DEFAULT_BUDGETS["max_cc"])
    parser.add_argument("--budget-depth", type=int, default=DEFAULT_BUDGETS["max_depth"])
    parser.add_argument(
        "--budget-working-set-tokens",
        type=int,
        default=DEFAULT_BUDGETS["max_working_set_tokens"],
    )
    return parser


def _dump_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)


def _resolve_ordinary_baseline_dest(raw_dest: Path) -> Path:
    """Validate un-resolved parent ancestors and leaf lstat before writing a baseline file."""
    if not raw_dest.name or raw_dest.name in (".", ".."):
        raise ValueError(f"ordinary non-symlink baseline destination required: {raw_dest}")
    checked_parent = check_ordinary_ancestors(
        raw_dest.parent,
        expect_dir=True,
        error_prefix="ordinary non-symlink baseline directory required",
    )
    candidate = checked_parent / raw_dest.name
    try:
        info = candidate.lstat()
    except FileNotFoundError:
        return checked_parent.resolve(strict=True) / raw_dest.name
    if not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary non-symlink baseline destination required: {candidate}")
    return checked_parent.resolve(strict=True) / raw_dest.name


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.check_budget and args.write_baseline is not None:
            raise ValueError("--check-budget and --write-baseline cannot be combined")
        if args.check_budget:
            if args.baseline is None:
                raise ValueError("--check-budget requires --baseline PATH")
            baseline = load_baseline(args.baseline)
            profile = profile_repository(
                args.root,
                package_rel=baseline["package_dir"],
                top_n=args.top,
                git_cochange=args.git_cochange,
                cochange_window=args.cochange_window,
                cochange_min=args.cochange_min,
            )
            result = check_budget(profile, baseline)
            if result["status"] != "passed":
                print(_dump_json(result), file=sys.stderr)
                return 1
            print(_dump_json(result))
            return 0

        profile = profile_repository(
            args.root,
            package_rel=args.package_dir,
            top_n=args.top,
            git_cochange=args.git_cochange,
            cochange_window=args.cochange_window,
            cochange_min=args.cochange_min,
        )
        if args.write_baseline is not None:
            budgets = {
                "max_file_lines": args.budget_lines,
                "max_cc": args.budget_cc,
                "max_depth": args.budget_depth,
                "max_working_set_tokens": args.budget_working_set_tokens,
            }
            baseline_doc = build_baseline(profile, budgets=budgets)
            dest = _resolve_ordinary_baseline_dest(args.write_baseline)
            encoded = (_dump_json(baseline_doc) + "\n").encode("utf-8")
            atomic_bytes(dest, encoded)
            print(
                _dump_json(
                    {
                        "format": BASELINE_FORMAT,
                        "status": "written",
                        "path": dest.as_posix(),
                        "allowance_count": len(baseline_doc["allowances"]),
                        "relationship_budgets": baseline_doc["relationship_budgets"],
                        "summary": baseline_doc["summary"],
                    }
                )
            )
            return 0

        print(_dump_json(profile))
        return 0
    except (OSError, ValueError) as exc:
        payload = {
            "format": PROFILE_FORMAT,
            "status": "error",
            "error": str(exc),
        }
        print(_dump_json(payload), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
