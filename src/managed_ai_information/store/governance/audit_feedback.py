"""Find a feedback revision which dropped outstanding adverse returns.

Usage:
  python -B -m managed_ai_information.store.governance.audit_feedback ENGINE RUNTIME FEEDBACK_ID
The optional --obligation check follows the verified retained revisions of a native record.
"""

import argparse
import importlib
import importlib.util
import json
import sys
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from importlib.machinery import ModuleSpec
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
ENGINE_ROOT = Path(__file__).resolve().parents[2]
_ISOLATED_ROOT_PACKAGES = frozenset(
    {"managed_ai_information", "core", "store", "library", "deployment"}
)


def _is_package_mounted(root: Path) -> bool:
    loaded = sys.modules.get("managed_ai_information")
    if loaded is None:
        return False
    loaded_file = getattr(loaded, "__file__", None)
    if loaded_file and Path(loaded_file).resolve() == (root / "__init__.py").resolve():
        return True
    return any(Path(p).resolve() == root for p in getattr(loaded, "__path__", ()))


@contextmanager
def _with_product_engine(product: Path) -> Generator[tuple[Path, bool]]:
    """Mount the requested engine root cleanly for the full audit operation, restoring on exit."""
    candidate = Path(product).resolve()
    root = candidate if (candidate / "store" / "system.py").is_file() else ENGINE_ROOT
    is_pkg = (root / "__init__.py").is_file()
    if is_pkg and _is_package_mounted(root):
        yield root, True
        return

    def _is_isolated(name: str) -> bool:
        return name.partition(".")[0] in _ISOLATED_ROOT_PACKAGES

    previous = {name: sys.modules.pop(name) for name in tuple(sys.modules) if _is_isolated(name)}
    saved_path = sys.path[:]
    try:
        excluded = {ENGINE_ROOT, ENGINE_ROOT.parent}
        filtered = [p for p in saved_path if Path(p).resolve() not in excluded]
        if is_pkg:
            sys.path[:] = filtered
            init = root / "__init__.py"
            spec = importlib.util.spec_from_file_location(
                "managed_ai_information",
                str(init),
                submodule_search_locations=[str(root)],
            )
            if spec is None:
                spec = ModuleSpec("managed_ai_information", None, is_package=True)
                spec.submodule_search_locations = [str(root)]
            module = importlib.util.module_from_spec(spec)
            sys.modules["managed_ai_information"] = module
            if spec.loader is not None:
                spec.loader.exec_module(module)
        else:
            sys.path[:] = [str(root), *filtered]
        importlib.invalidate_caches()
        yield root, is_pkg
    finally:
        for name in tuple(sys.modules):
            if _is_isolated(name):
                del sys.modules[name]
        sys.modules.update(previous)
        sys.path[:] = saved_path
        importlib.invalidate_caches()


@contextmanager
def _admitted_history(
    product: Path,
    root: Path,
    ident: str,
) -> Generator[tuple[Iterator[dict[str, Any]], bool]]:
    with _with_product_engine(product) as (engine_root, is_pkg):
        prefix = "managed_ai_information." if is_pkg else ""
        ownership_mod = importlib.import_module(prefix + "core.ownership")
        storage_mod = importlib.import_module(prefix + "core.storage")
        system_mod = importlib.import_module(prefix + "store.system")
        expected_system = (engine_root / "store" / "system.py").resolve(strict=True)
        system_file = system_mod.__file__
        if system_file is None or Path(system_file).resolve(strict=True) != expected_system:
            raise ValueError("loaded engine system module differs from requested product")

        resolved_root = ownership_mod.external_root(root)
        lock_path = (
            ownership_mod.external_root(ownership_mod.NativeLayout(resolved_root).exchange)
            / "owner.lock"
        )
        with storage_mod.owner_admission(
            lambda stack: stack.enter_context(
                storage_mod.shared(lock_path, allow_readonly_open=True)
            ),
            deadline=storage_mod.admission_deadline(),
        ):
            store = system_mod.System(resolved_root).record_store

            def _pages() -> Iterator[dict[str, Any]]:
                for page in store.history_pages(ident):
                    if page.get("retention_boundary"):
                        raise ValueError(
                            "Historical audit stops at the declared current-use retention "
                            "boundary; absence of earlier revisions is not a clean historical "
                            "finding"
                        )
                    yield from page["records"]

            yield _pages(), is_pkg


def history(product: Path, root: Path, ident: str) -> list[dict[str, Any]]:
    with _admitted_history(product, root, ident) as (records, _):
        return list(records)


def scan(product: Path, root: Path, ident: str) -> dict[str, Any]:
    with _admitted_history(product, root, ident) as (records, is_pkg):
        current = next(records)
        feedback_mod = importlib.import_module(
            ("managed_ai_information." if is_pkg else "") + "store.governance.feedback"
        )
        Feedback, KIND = feedback_mod.Feedback, feedback_mod.KIND
        if current["kind"] != KIND:
            raise ValueError("record does not own a feedback use")

        head_revision = current["revision"]
        gaps: list[dict[str, Any]] = []
        for previous in records:
            content = current["content"]
            if content["state"] == "open" and "revision_reason" in content:
                carried = {r["return_number"] for r in content.get("carried_returns", [])}
                pending = Feedback.outstanding(previous["content"])
                missing = pending.keys() - carried
                if missing:
                    gaps.append(
                        {
                            "revision": current["revision"],
                            "prior_revision": previous["revision"],
                            "returns": [
                                {
                                    "number": n,
                                    "observation": pending[n]["observation"],
                                    "interpretation": pending[n]["interpretation"],
                                }
                                for n in sorted(missing)
                            ],
                        }
                    )
            current = previous

        gaps.reverse()
        return {
            "feedback": ident,
            "through_revision": head_revision,
            "unexplained_drops": gaps,
            "scope": (
                "Historical representational gaps. Inspect the pinned return and test the "
                "current receiving use."
            ),
        }


def scan_obligation(product: Path, root: Path, ident: str) -> dict[str, Any]:
    with _admitted_history(product, root, ident) as (records, _):
        current = next(records)
        head_revision = current["revision"]
        findings: list[dict[str, Any]] = []
        inactive_revisions: list[int] = []

        def owed(record: dict[str, Any]) -> bool:
            if record["kind"] != "obligation":
                return False
            content = record["content"]
            return not (
                isinstance(content, dict)
                and content.get("disposition") in ("fulfilled", "released")
                and isinstance(content.get("account"), str)
                and content["account"].strip()
            )

        while True:
            previous = next(records, None)
            issues = []
            if owed(current):
                if current["active"] is False:
                    inactive_revisions.append(current["revision"])
                if isinstance(current["content"], dict) and current["content"].get(
                    "disposition"
                ) in ("fulfilled", "released"):
                    issues.append("closure had no account")

            parent = current["previous_object"]
            if previous is None:
                if issues:
                    findings.append({"revision": 1, "observations": issues})
                break

            if owed(previous):
                if current["kind"] != "obligation":
                    issues.append("retyped before accounted closure")
                elif owed(current):
                    basis = {r["path"]: r["sha256"] for r in current["basis"]}
                    content = current["content"]
                    change = content.get("basis_change") if isinstance(content, dict) else None
                    accounted = (
                        isinstance(change, dict)
                        and type(change.get("from_revision")) is int
                        and change["from_revision"] == previous["revision"]
                        and isinstance(change.get("account"), str)
                        and bool(change["account"].strip())
                    )
                    if not accounted and any(
                        r["path"] not in basis or basis[r["path"]] != r["sha256"]
                        for r in previous["basis"]
                    ):
                        issues.append("earlier evidence removed or replaced without account")

            if issues:
                findings.append(
                    {
                        "revision": current["revision"],
                        "observations": issues,
                        "predecessor_object": parent,
                    }
                )
            current = previous

        return {
            "record": ident,
            "through_revision": head_revision,
            "history": list(reversed(findings)),
            "inactive_for_navigation": list(reversed(inactive_revisions)),
            "scope": (
                "Historical transitions. Inactive revisions are informational and are excluded "
                "from the findings. Ordinary work retains typed open duties."
            ),
        }


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    cli.add_argument("product", type=Path)
    cli.add_argument("root", type=Path)
    cli.add_argument("record")
    cli.add_argument(
        "--obligation",
        action="store_true",
        help="inspect obligation transitions including former obligations",
    )
    a = cli.parse_args()
    inspect = scan_obligation if a.obligation else scan
    try:
        result = inspect(a.product.resolve(), a.root.resolve(), a.record)
    except RuntimeError as exc:
        result_fn = getattr(exc, "result", None)
        if type(exc).__name__ == "AdmissionContended" and callable(result_fn):
            print(json.dumps(result_fn("audit-feedback")), file=sys.stderr)
            raise SystemExit(75) from None
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        cli.exit(
            2, json.dumps({"status": "unresolved", "record": a.record, "error": str(exc)}) + "\n"
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))
