"""Component and symbol-level blast-radius impact analysis."""

import ast
import os
import re
import shutil
import stat
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

from managed_ai_information.core.ownership import relative_path
from managed_ai_information.core.storage import MAX_BYTES, is_ordinary_dir_stat, read_bytes
from managed_ai_information.platform import check_ordinary_ancestors, is_ordinary_file_stat

IMPACT_REPORT_FORMAT = "managed-ai-impact-report/1"

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP_WORDS: frozenset[str] = frozenset(
    _TOKEN_RE.findall(
        "src py test tests scripts managed ai information mod facade pkg risk review "
        "and the for with from into that this not are must when all any one use get set run init"
    )
)
_SUBSYSTEM_CRW_HINTS: dict[str, tuple[str, ...]] = {
    "deployment": ("deployment", "install", "upgrade", "rollback", "isolation", "boundary", "pin"),
    "storage": ("persistence", "atomic", "durability", "corruption", "fsync", "symlink", "disk"),
    "membus": ("migration", "protocol", "network", "transport", "daemon", "parity", "retry"),
    "records": ("schema", "revision", "integrity", "collision", "index", "archival", "identity"),
    "query": ("discovery", "graph", "traversal", "bounded", "latency", "projection", "index"),
    "composition": ("boundary", "contract", "propagation", "consumer", "edition", "manifest"),
    "representation": ("witness", "rollback", "boundary", "symlink", "atomic", "contract"),
    "material_retention": ("archive", "zip", "compression", "decompression", "retention", "lock"),
    "continuing_work": ("handoff", "recovery", "resume", "lock", "custody", "ownership"),
    "signal_field": ("queue", "dispatch", "allowlist", "worker", "timeout", "sqlite"),
    "consolidation": ("etl", "ingestion", "drift", "idempotent", "coverage", "manifest"),
    "platform": ("windows", "posix", "symlink", "reparse", "filesystem", "lock", "portability"),
}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pick(data: dict[str, Any], keys: tuple[str, ...], **extra: Any) -> dict[str, Any]:
    row = {k: data.get(k) for k in keys}
    row.update(extra)
    return row


def _record_id_for(prefix: str, module_name: str) -> str:
    parts = module_name.split(".")
    trimmed = ".".join(parts[1:]) if len(parts) > 1 else parts[0]
    cleaned = re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9_-]+", "-", trimmed.lower()).strip("-"))
    return f"{prefix}-{cleaned}" if cleaned else prefix


def _path_to_module_name(rel_path: str, package_rel: str) -> str | None:
    norm_pkg = relative_path(package_rel).rstrip("/")
    pkg_name = norm_pkg.rsplit("/", 1)[-1]
    if not rel_path.endswith(".py"):
        return None
    if rel_path.startswith(norm_pkg + "/"):
        inner = rel_path[len(norm_pkg) + 1 :]
    elif rel_path == norm_pkg + ".py":
        return pkg_name
    else:
        return None
    parts = inner.split("/")
    parts = parts[:-1] if parts[-1] == "__init__.py" else [*parts[:-1], parts[-1][:-3]]
    return ".".join([pkg_name, *parts]) if parts else pkg_name


def _walk_ordinary_root(
    checked_repo: Path, rel_root: str, seen: set[str], found: list[tuple[Path, str]]
) -> None:
    safe_rel = relative_path(rel_root)
    base = checked_repo / safe_rel
    if not base.exists() and not base.is_symlink():
        return
    checked_base = check_ordinary_ancestors(
        base, expect_dir=True, error_prefix=f"ordinary scan dir required ({safe_rel})"
    ).resolve(strict=True)
    if not checked_base.is_relative_to(checked_repo):
        raise ValueError(f"scan dir escapes repository root: {safe_rel}")
    stack = [(checked_base, safe_rel)]
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
            child, child_rel = Path(entry.path), f"{cur_rel}/{name}"
            if stat.S_ISDIR(info.st_mode):
                if not is_ordinary_dir_stat(info):
                    raise ValueError(f"ordinary dir required: {entry.path}")
                stack.append((child, child_rel))
            elif name.endswith(".py"):
                if not is_ordinary_file_stat(info):
                    raise ValueError(f"ordinary Python file required: {entry.path}")
                if child_rel not in seen:
                    seen.add(child_rel)
                    found.append((child, child_rel))


def resolve_git_diff_targets(repo_root: Path, git_diff_rev: str) -> list[str]:
    """Return sorted repository-relative paths changed relative to git_diff_rev."""
    rev = git_diff_rev.strip()
    if not rev or rev.startswith("-"):
        raise ValueError(f"invalid --git-diff revision: {git_diff_rev!r}")
    checked_repo = check_ordinary_ancestors(
        repo_root, expect_dir=True, error_prefix="ordinary repository root required"
    ).resolve(strict=True)
    git_bin = shutil.which("git")
    if not git_bin:
        raise ValueError("git executable not found in PATH")
    cmd = [git_bin, "-C", str(checked_repo), "diff", "--name-only", rev, "--"]
    proc = subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, encoding="utf-8", check=False, timeout=30
    )
    if proc.returncode != 0:
        raise ValueError(f"git diff failed for {rev!r}: {proc.stderr.strip() or proc.returncode}")
    return sorted(
        {
            relative_path(ln.strip().replace("\\", "/"))
            for ln in proc.stdout.splitlines()
            if ln.strip()
        }
    )


def _extract_all_exports(tree: ast.Module) -> set[str]:
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets
        ):
            try:
                val = ast.literal_eval(stmt.value)
            except TypeError, ValueError:
                return set()
            if isinstance(val, (list, tuple)) and all(isinstance(x, str) for x in val):
                return set(val)
    return set()


def _collect_stmt_symbols(
    stmt: ast.stmt, all_names: set[str], symbols: dict[str, dict[str, Any]]
) -> None:
    def _add(name: str, kind: str, line: int) -> None:
        if (name.startswith("_") and name not in all_names) or name in symbols:
            return
        symbols[name] = {
            "name": name,
            "kind": kind,
            "line": line,
            "exported_via_all": name in all_names,
        }

    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        _add(stmt.name, "function", stmt.lineno)
    elif isinstance(stmt, ast.ClassDef):
        _add(stmt.name, "class", stmt.lineno)
    elif isinstance(stmt, ast.Assign):
        for tgt in stmt.targets:
            if isinstance(tgt, ast.Name) and tgt.id != "__all__":
                _add(tgt.id, "constant" if tgt.id.isupper() else "variable", stmt.lineno)
    elif (
        isinstance(stmt, ast.AnnAssign)
        and isinstance(stmt.target, ast.Name)
        and stmt.target.id != "__all__"
    ):
        name = stmt.target.id
        _add(name, "constant" if name.isupper() else "variable", stmt.lineno)
    elif isinstance(stmt, ast.ImportFrom):
        for alias in stmt.names:
            bound = alias.asname or alias.name
            if alias.name != "*" and (
                bound in all_names or alias.asname == alias.name or bound.isupper()
            ):
                _add(bound, "reexport", stmt.lineno)


def extract_public_symbols(file_path: Path, rel_path: str) -> list[dict[str, Any]]:
    """Extract top-level public functions, classes, constants, and re-exports from a .py file."""
    tree = ast.parse(read_bytes(file_path, MAX_BYTES).decode("utf-8"), filename=rel_path)
    all_names = _extract_all_exports(tree)
    symbols: dict[str, dict[str, Any]] = {}
    for stmt in tree.body:
        _collect_stmt_symbols(stmt, all_names, symbols)
    return sorted(symbols.values(), key=lambda row: (row["line"], row["name"]))


def _dotted_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _resolve_relative_base(cur_mod: str | None, is_init: bool, level: int, mod: str | None) -> str:
    if level <= 0 or not cur_mod:
        return mod or ""
    parts = cur_mod.split(".")
    base = parts if is_init else parts[:-1]
    drop = max(level - 1, 0)
    prefix = base[: len(base) - drop] if drop <= len(base) else []
    return ".".join([*prefix, *mod.split(".")]) if mod else ".".join(prefix)


class _CallerVisitor(ast.NodeVisitor):
    """AST visitor tracing imports, attribute reads, calls, and mock.patch targets."""

    def __init__(
        self,
        rel_path: str,
        cur_mod: str | None,
        is_init: bool,
        target_symbols: dict[str, tuple[str, set[str]]],
    ) -> None:
        self.rel_path, self.cur_mod, self.is_init = rel_path, cur_mod, is_init
        self.target_symbols = target_symbols
        self.scope: list[str] = []
        self.mod_aliases: dict[str, str] = {}
        self.sym_aliases: dict[str, tuple[str, str]] = {}
        self.hits: list[dict[str, Any]] = []
        self._seen: set[tuple[int, str, str, str]] = set()

    def _record(self, line: int, target_mod: str, symbol: str, ref_kind: str) -> None:
        key = (line, target_mod, symbol, ref_kind)
        if key in self._seen:
            return
        self._seen.add(key)
        row = {
            "file": self.rel_path,
            "line": line,
            "symbol": symbol,
            "ref_kind": ref_kind,
            "enclosing": ".".join(self.scope) if self.scope else "<module>",
            "target_file": self.target_symbols[target_mod][0],
            "target_module": target_mod,
        }
        self.hits.append(row)

    def _scoped(self, name: str, node: ast.AST) -> None:
        self.scope.append(name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scoped(node.name, node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node.name, node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scoped(node.name, node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name in self.target_symbols:
                self.mod_aliases[alias.asname or alias.name] = alias.name
                self._record(node.lineno, alias.name, "<module>", "import")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = _resolve_relative_base(self.cur_mod, self.is_init, node.level, node.module)
        for alias in node.names:
            if alias.name == "*":
                if base in self.target_symbols:
                    self._record(node.lineno, base, "*", "import")
                continue
            bound, cand = alias.asname or alias.name, f"{base}.{alias.name}" if base else alias.name
            if cand in self.target_symbols:
                self.mod_aliases[bound] = cand
                self._record(node.lineno, cand, "<module>", "import")
            elif base in self.target_symbols and alias.name in self.target_symbols[base][1]:
                self.sym_aliases[bound] = (base, alias.name)
                self._record(node.lineno, base, alias.name, "import")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        dotted = _dotted_name(node.value)
        if dotted in self.mod_aliases:
            tmod = self.mod_aliases[dotted]
            if node.attr in self.target_symbols[tmod][1]:
                self._record(node.lineno, tmod, node.attr, "attribute")
        self.generic_visit(node)

    def _record_patch_str(self, line: int, dotted: str) -> None:
        cleaned = dotted.strip()
        for tmod, (_, syms) in self.target_symbols.items():
            if cleaned.startswith(tmod + "."):
                head = cleaned[len(tmod) + 1 :].split(".", 1)[0]
                if head in syms:
                    self._record(line, tmod, head, "patch")

    def visit_Call(self, node: ast.Call) -> None:
        callee = _dotted_name(node.func)
        if callee in self.sym_aliases:
            tmod, sym = self.sym_aliases[callee]
            self._record(node.lineno, tmod, sym, "call")
        elif (callee == "patch" or callee.endswith(".patch")) and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                self._record_patch_str(node.lineno, first.value)
        elif callee.endswith("patch.object") and len(node.args) >= 2:
            obj_name, attr_arg = _dotted_name(node.args[0]), node.args[1]
            if obj_name in self.mod_aliases and isinstance(attr_arg, ast.Constant):
                val = attr_arg.value
                tmod = self.mod_aliases[obj_name]
                if isinstance(val, str) and val in self.target_symbols[tmod][1]:
                    self._record(node.lineno, tmod, val, "patch_object")
        self.generic_visit(node)


def trace_symbol_callers(
    checked_repo: Path,
    target_symbols: dict[str, tuple[str, set[str]]],
    *,
    package_rel: str = "src/managed_ai_information",
    scan_roots: tuple[str, ...] = ("src", "scripts", "tests"),
) -> list[dict[str, Any]]:
    """Trace unchanged symbol-level callers across src/, scripts/, and tests/."""
    if not target_symbols:
        return []
    target_files = {rel for rel, _ in target_symbols.values()}
    files: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for rel_root in scan_roots:
        _walk_ordinary_root(checked_repo, rel_root, seen, files)
    files.sort(key=lambda item: item[1])

    all_hits: list[dict[str, Any]] = []
    for file_path, rel_path in files:
        if rel_path in target_files:
            continue
        try:
            tree = ast.parse(read_bytes(file_path, MAX_BYTES).decode("utf-8"), filename=rel_path)
        except SyntaxError, UnicodeDecodeError:
            continue
        cur_mod = _path_to_module_name(rel_path, package_rel)
        visitor = _CallerVisitor(rel_path, cur_mod, file_path.name == "__init__.py", target_symbols)
        visitor.visit(tree)
        all_hits.extend(visitor.hits)
    all_hits.sort(
        key=lambda r: (r["target_file"], r["symbol"], r["file"], r["line"], r["ref_kind"])
    )
    return all_hits


def _build_store_index(
    system: Any,
) -> tuple[dict[str, dict[str, Any]], dict[str, str], dict[str, str]]:
    records = {
        rec["id"]: rec for rec in system.record_store.scan_records() if rec.get("active", True)
    }
    mod_by_path: dict[str, str] = {}
    facade_by_path: dict[str, str] = {}
    for rec_id, rec in records.items():
        path = _as_dict(rec.get("content")).get("path")
        if isinstance(path, str):
            if rec.get("kind") == "code-module":
                mod_by_path[path] = rec_id
            elif rec.get("kind") == "code-facade":
                facade_by_path[path] = rec_id
    return records, mod_by_path, facade_by_path


def _collect_annotations(
    records: dict[str, dict[str, Any]], impacted: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    suites: list[dict[str, Any]] = []
    unknowns: list[dict[str, Any]] = []
    hotspots: list[dict[str, Any]] = []
    suite_keys = ("path", "boundary_class", "assertion_count")
    unk_keys = ("module_id", "path", "reason", "transitive_only")
    for rec_id, rec in sorted(records.items()):
        kind = rec.get("kind")
        if kind not in ("test-suite", "verification-unknown", "semantic-hotspot"):
            continue
        rels = rec.get("relations", ())
        hit = sorted(
            {str(r["target"]) for r in rels if isinstance(r, dict) and r.get("target") in impacted}
        )
        if not hit:
            continue
        c = _as_dict(rec.get("content"))
        if kind == "test-suite":
            suites.append(_pick(c, suite_keys, id=rec_id, covers_targets=hit))
        elif kind == "verification-unknown":
            unknowns.append(_pick(c, unk_keys, id=rec_id))
        else:
            hotspots.append(
                _pick(
                    c,
                    ("module_id", "path"),
                    id=rec_id,
                    reasons=c.get("reasons", []),
                    metrics=c.get("metrics", {}),
                )
            )
    return suites, unknowns, hotspots


def _collect_blast_radius(
    target_mod_ids: list[str], records: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    incoming: dict[str, set[str]] = defaultdict(set)
    components: set[str] = set()
    target_set = set(target_mod_ids)
    for rec_id, rec in records.items():
        kind = rec.get("kind")
        for rel in rec.get("relations", ()):
            if not isinstance(rel, dict):
                continue
            r_label, r_tgt = rel.get("relation"), str(rel.get("target", ""))
            if kind in ("code-module", "code-facade") and r_label in ("depends-on", "instantiates"):
                incoming[r_tgt].add(rec_id)
            elif (
                rec_id in target_set
                and r_label == "maps-to"
                and records.get(r_tgt, {}).get("kind") == "software-component"
            ):
                components.add(r_tgt)

    hop1 = {dep for tid in target_set for dep in incoming.get(tid, ())} - target_set
    hop2 = {dep for h1 in hop1 for dep in incoming.get(h1, ())} - (target_set | hop1)
    suites, unknowns, hotspots = _collect_annotations(records, target_set | hop1)

    def _pin(rid: str) -> dict[str, Any]:
        rec = records.get(rid, {})
        return _pick(_as_dict(rec.get("content")), ("module", "path"), id=rid, kind=rec.get("kind"))

    return {
        "hop_1_dependents": [_pin(rid) for rid in sorted(hop1)],
        "hop_2_dependents": [_pin(rid) for rid in sorted(hop2)],
        "components": sorted(components),
        "exercising_test_suites": suites,
        "verification_unknowns": unknowns,
        "semantic_hotspots": hotspots,
    }


def _extract_query_terms(
    targets: list[str], per_target: list[dict[str, Any]], blast_radius: dict[str, Any]
) -> set[str]:
    terms: set[str] = set()
    for raw in (*targets, *(str(c) for c in blast_radius.get("components", ()))):
        for tok in _TOKEN_RE.findall(raw.lower()):
            if len(tok) >= 3 and tok not in _STOP_WORDS:
                terms.add(tok)
                terms.update(_SUBSYSTEM_CRW_HINTS.get(tok, ()))
    for item in per_target:
        for sym in item.get("public_symbols", ()):
            for tok in _TOKEN_RE.findall(str(sym["name"]).lower()):
                if len(tok) >= 4 and tok not in _STOP_WORDS:
                    terms.add(tok)
    if blast_radius.get("semantic_hotspots"):
        terms.update(("complexity", "boundary", "coupling", "refactor"))
    if blast_radius.get("verification_unknowns"):
        terms.update(("verification", "coverage", "mock", "boundary"))
    return terms


def _analyze_crw_risks(
    crw_system: Any, query_terms: set[str], *, max_constituents: int = 8, max_controls: int = 12
) -> dict[str, Any]:
    crw_recs = {r["id"]: r for r in crw_system.record_store.scan_records() if r.get("active", True)}
    ctrls_by_dim: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in crw_recs.values():
        if rec.get("kind") != "crw-control":
            continue
        for rel in rec.get("relations", ()):
            tgt = str(rel.get("target", "")) if isinstance(rel, dict) else ""
            if (
                isinstance(rel, dict)
                and rel.get("relation") == "supports"
                and tgt.startswith("crw-dim-")
            ):
                ctrls_by_dim[tgt].append(rec)

    dim_fields = ("slug", "concern", "cue", "family", "title", "dimension_id")
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for rec_id, rec in sorted(crw_recs.items()):
        if rec.get("kind") != "crw-constituent":
            continue
        c = _as_dict(rec.get("content"))
        hay = " ".join(str(c.get(k, "")) for k in dim_fields).lower()
        matched = sorted(query_terms & set(_TOKEN_RE.findall(hay)))
        if matched:
            ctrls = sorted(str(x["id"]) for x in ctrls_by_dim.get(rec_id, ()))
            row = _pick(
                c,
                ("family", "concern", "cue"),
                id=rec_id,
                score=len(matched),
                matched_terms=matched,
                controls=ctrls,
            )
            scored.append((len(matched), rec_id, row))

    scored.sort(key=lambda item: (-item[0], item[1]))
    top_dims = [row for _, _, row in scored[:max_constituents]]
    by_id: dict[str, dict[str, Any]] = {}
    dim_sets: dict[str, set[str]] = defaultdict(set)
    ctrl_keys = ("control_id", "escape_class", "purpose")
    for dim in top_dims:
        dim_id = str(dim["id"])
        for ctrl in ctrls_by_dim.get(dim_id, ()):
            cid = str(ctrl["id"])
            if cid not in by_id:
                by_id[cid] = _pick(_as_dict(ctrl.get("content")), ctrl_keys, id=cid)
            dim_sets[cid].add(dim_id)
    ranked = [dict(e, matched_constituents=sorted(dim_sets[cid])) for cid, e in by_id.items()]
    ranked.sort(key=lambda row: (-len(row["matched_constituents"]), str(row["id"])))
    return {"enabled": True, "constituents": top_dims, "controls": ranked[:max_controls]}


def _resolve_target_entry(
    checked_repo: Path,
    rel: str,
    package_rel: str,
    records: dict[str, dict[str, Any]],
    mod_by_path: dict[str, str],
    facade_by_path: dict[str, str],
) -> tuple[dict[str, Any], list[str], tuple[str, set[str]] | None]:
    raw_target = checked_repo / rel
    deleted = not raw_target.exists() and not raw_target.is_symlink()
    file_path: Path | None = None
    if deleted:
        anc = raw_target.parent
        while anc != checked_repo and not anc.exists() and not anc.is_symlink():
            anc = anc.parent
        checked_anc = check_ordinary_ancestors(
            anc, expect_dir=True, error_prefix=f"ordinary target parent required ({rel})"
        ).resolve(strict=True)
        if not checked_anc.is_relative_to(checked_repo):
            raise ValueError(f"ordinary repository target file required: {rel}")
    else:
        file_path = check_ordinary_ancestors(
            raw_target, expect_dir=False, error_prefix=f"ordinary target required ({rel})"
        ).resolve(strict=True)
        if not file_path.is_relative_to(checked_repo) or not is_ordinary_file_stat(
            file_path.lstat()
        ):
            raise ValueError(f"ordinary repository target file required: {rel}")
    mod_name = _path_to_module_name(rel, package_rel)
    mod_id = mod_by_path.get(rel) or (_record_id_for("mod", mod_name) if mod_name else None)
    facade_id = facade_by_path.get(rel)
    if (
        facade_id is None
        and mod_name
        and (rel.endswith("/__init__.py") or mod_name.endswith(".domain_api"))
    ):
        cand = _record_id_for("facade", mod_name)
        facade_id = cand if cand in records else None
    found_ids = [rid for rid in (mod_id, facade_id) if rid and rid in records]
    pub_syms = extract_public_symbols(file_path, rel) if file_path and rel.endswith(".py") else []
    sym_entry = (mod_name, {r["name"] for r in pub_syms}) if (mod_name and pub_syms) else None
    entry = {
        "path": rel,
        "deleted": deleted,
        "module": mod_name,
        "module_id": mod_id if (mod_id and mod_id in records) else None,
        "facade_id": facade_id,
        "public_symbols": pub_syms,
    }
    return entry, found_ids, sym_entry


def analyze_impact(
    repo_root: Path,
    system: Any,
    targets: list[str],
    *,
    package_rel: str = "src/managed_ai_information",
    scan_roots: tuple[str, ...] = ("src", "scripts", "tests"),
    crw_system: Any | None = None,
    max_crw_constituents: int = 8,
    max_crw_controls: int = 12,
) -> dict[str, Any]:
    """Compute component, symbol-caller, artifact-graph, and optional CRW risk impact report."""
    if not targets:
        raise ValueError("at least one target path is required for impact analysis")
    checked_repo = check_ordinary_ancestors(
        repo_root, expect_dir=True, error_prefix="ordinary repository root required"
    ).resolve(strict=True)
    norm_targets = sorted({relative_path(t.replace("\\", "/")) for t in targets})
    records, mod_by_path, facade_by_path = _build_store_index(system)

    target_symbols: dict[str, tuple[str, set[str]]] = {}
    target_mod_ids: list[str] = []
    per_target: list[dict[str, Any]] = []
    for rel in norm_targets:
        entry, found_ids, sym_entry = _resolve_target_entry(
            checked_repo, rel, package_rel, records, mod_by_path, facade_by_path
        )
        per_target.append(entry)
        target_mod_ids.extend(found_ids)
        if sym_entry is not None:
            target_symbols[sym_entry[0]] = (rel, sym_entry[1])

    callers = trace_symbol_callers(
        checked_repo, target_symbols, package_rel=package_rel, scan_roots=scan_roots
    )
    blast = _collect_blast_radius(sorted(set(target_mod_ids)), records)
    crw_risks: dict[str, Any] = {"enabled": False, "constituents": [], "controls": []}
    if crw_system is not None:
        crw_risks = _analyze_crw_risks(
            crw_system,
            _extract_query_terms(norm_targets, per_target, blast),
            max_constituents=max_crw_constituents,
            max_controls=max_crw_controls,
        )
    summary = {
        "target_count": len(norm_targets),
        "public_symbol_count": sum(len(t["public_symbols"]) for t in per_target),
        "unchanged_caller_count": len(callers),
        "patch_site_count": sum(1 for h in callers if h["ref_kind"] in ("patch", "patch_object")),
        "hop_1_dependent_count": len(blast["hop_1_dependents"]),
        "hop_2_dependent_count": len(blast["hop_2_dependents"]),
        "exercising_test_suite_count": len(blast["exercising_test_suites"]),
        "verification_unknown_count": len(blast["verification_unknowns"]),
        "semantic_hotspot_count": len(blast["semantic_hotspots"]),
        "crw_constituent_count": len(crw_risks["constituents"]),
        "crw_control_count": len(crw_risks["controls"]),
    }
    return {
        "format": IMPACT_REPORT_FORMAT,
        "repo": str(checked_repo),
        "store": str(system.root),
        "crw_store": str(crw_system.root) if crw_system is not None else None,
        "targets": norm_targets,
        "summary": summary,
        "per_target": per_target,
        "unchanged_callers": callers,
        "blast_radius": blast,
        "crw_risks": crw_risks,
    }
