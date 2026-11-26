"""Dual-OS unit and contract tests for image_lifecycle.py and Buildkite pipelines."""

import ast
import io
import json
import os
import subprocess
import sys
import tokenize
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import REPO, cleanup_tempdir

SCRIPT = REPO / "scripts/ci/image_lifecycle.py"
PIPELINE = REPO / ".buildkite/pipeline.yml"
NIGHTLY = REPO / ".buildkite/pipeline-nightly.yml"

if str(REPO / "scripts/ci") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts/ci"))
import image_lifecycle  # noqa: E402


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    )


class ImageLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="managed-ai-ci-")
        self.addCleanup(lambda: cleanup_tempdir(self._tmp))
        self.root = Path(self._tmp.name)

    def _make_dir_link(self, link_path: Path, target_dir: Path) -> None:
        if os.name == "nt":
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_path), str(target_dir)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.addCleanup(
                lambda p=link_path: subprocess.run(
                    ["cmd", "/c", "rmdir", str(p)], capture_output=True
                )
            )
        else:
            os.symlink(target_dir, link_path)

    def _run_cli(self, *args: str, expected_rc: int = 0) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONPATH", None)
        proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=30,
        )
        self.assertEqual(
            proc.returncode, expected_rc, f"args={args}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
        return proc

    def test_context_hash_determinism_ignores_docs_tests_and_rejects_links(self) -> None:
        repo = self.root / "mini-repo"
        (repo / "src/managed_ai_information").mkdir(parents=True)
        (repo / "containers").mkdir()
        (repo / "docs").mkdir()
        (repo / "tests").mkdir()
        (repo / "pyproject.toml").write_text('[project]\nname = "managed-ai"\n', encoding="utf-8")
        (repo / "src/managed_ai_information/__init__.py").write_text(
            '__version__ = "0.5.0"\n', encoding="utf-8"
        )
        (repo / "containers/Containerfile").write_text(
            "FROM python:3.15.2-slim\n", encoding="utf-8"
        )

        first = json.loads(self._run_cli("context-hash", "--root", str(repo)).stdout)
        self.assertEqual(first["format"], image_lifecycle.CONTEXT_HASH_FORMAT)
        self.assertEqual(first["file_count"], 3)

        # Changes outside build inputs (docs/, tests/, __pycache__) must NOT change context_sha256.
        (repo / "docs/README.md").write_text("# Doc change\n", encoding="utf-8")
        (repo / "tests/test_dummy.py").write_text("# Test change\n", encoding="utf-8")
        (repo / "src/managed_ai_information/__pycache__").mkdir()
        (repo / "src/managed_ai_information/__pycache__/mod.cpython-312.pyc").write_bytes(b"pyc")
        second = json.loads(self._run_cli("context-hash", "--root", str(repo)).stdout)
        self.assertEqual(second["context_sha256"], first["context_sha256"])

        # Editing src/managed_ai_information/ or Containerfile MUST change context_sha256.
        (repo / "src/managed_ai_information/__init__.py").write_text(
            '__version__ = "0.5.1"\n', encoding="utf-8"
        )
        third = json.loads(self._run_cli("context-hash", "--root", str(repo)).stdout)
        self.assertNotEqual(third["context_sha256"], first["context_sha256"])

        # Symlink / NTFS junction root must be rejected with exit 2 and no traceback.
        linked = self.root / "linked-repo"
        self._make_dir_link(linked, repo)
        bad = self._run_cli("context-hash", "--root", str(linked), expected_rc=2)
        self.assertNotIn("Traceback", bad.stderr)
        self.assertIn("image lifecycle error:", bad.stderr)

        # C60-2: dangling symlink or junction at containers/ must be rejected with exit 2.
        repo_dangling = self.root / "dangling-containers-repo"
        (repo_dangling / "src/managed_ai_information").mkdir(parents=True)
        (repo_dangling / "pyproject.toml").write_text(
            '[project]\nname = "managed-ai"\n', encoding="utf-8"
        )
        (repo_dangling / "src/managed_ai_information/__init__.py").write_text(
            '__version__ = "0.5.0"\n', encoding="utf-8"
        )
        missing_target = self.root / "gone-containers-dir"
        if os.name == "nt":
            missing_target.mkdir()
            self._make_dir_link(repo_dangling / "containers", missing_target)
            missing_target.rmdir()
        else:
            os.symlink(missing_target, repo_dangling / "containers")
        bad_cf = self._run_cli("context-hash", "--root", str(repo_dangling), expected_rc=2)
        self.assertNotIn("Traceback", bad_cf.stderr)
        self.assertIn("ordinary containers directory required", bad_cf.stderr)

    def test_tag_plan_channels_forbids_latest_and_validates_inputs(self) -> None:
        sha_full = "a1b2c3d4e5f60123456789abcdef0123456789ab"
        sha12 = sha_full[:12]

        local = json.loads(
            self._run_cli("tag-plan", "--channel", "local", "--git-sha", sha_full).stdout
        )
        self.assertFalse(local["push_remote"])
        self.assertEqual(local["tags"], [f"local-{sha12}"])
        self.assertTrue(local["forbids_latest"])

        pr_default = json.loads(
            self._run_cli(
                "tag-plan",
                "--channel",
                "pr",
                "--git-sha",
                sha_full,
                "--pr-number",
                "42",
            ).stdout
        )
        self.assertFalse(pr_default["push_remote"])
        self.assertEqual(pr_default["tags"], [f"pr-42-{sha12}"])
        self.assertEqual(pr_default["ttl_days"], 7)

        pr_push = json.loads(
            self._run_cli(
                "tag-plan",
                "--channel",
                "pr",
                "--git-sha",
                sha_full,
                "--pr-number",
                "42",
                "--allow-pr-push",
            ).stdout
        )
        self.assertTrue(pr_push["push_remote"])
        self.assertEqual(pr_push["tags"], [f"pr-42-{sha12}"])

        main_plan = json.loads(
            self._run_cli("tag-plan", "--channel", "main", "--git-sha", sha_full).stdout
        )
        self.assertTrue(main_plan["push_remote"])
        self.assertEqual(main_plan["promote_by"], "digest")
        self.assertEqual(main_plan["tags"], [f"sha-{sha12}"])

        # C60B-1: pipe real 64-char context_sha256 from context-hash into tag-plan --context-sha.
        real_ctx = json.loads(self._run_cli("context-hash", "--root", str(REPO)).stdout)
        ctx_sha64 = real_ctx["context_sha256"]
        self.assertEqual(len(ctx_sha64), 64)

        rel = json.loads(
            self._run_cli(
                "tag-plan",
                "--channel",
                "release",
                "--git-sha",
                sha_full,
                "--version",
                "1.2.3",
                "--context-sha",
                ctx_sha64,
            ).stdout
        )
        self.assertTrue(rel["push_remote"])
        self.assertEqual(rel["promotion_tags"], ["v1.2.3", f"sha-{sha12}"])
        self.assertEqual(rel["context_tag"], f"ctx-{ctx_sha64[:12]}")
        self.assertEqual(rel["tags"], ["v1.2.3", f"sha-{sha12}", f"ctx-{ctx_sha64[:12]}"])

        main_ctx = json.loads(
            self._run_cli(
                "tag-plan",
                "--channel",
                "main",
                "--git-sha",
                sha_full,
                "--context-sha",
                ctx_sha64,
            ).stdout
        )
        self.assertEqual(main_ctx["promotion_tags"], [f"sha-{sha12}"])
        self.assertEqual(main_ctx["tags"], [f"sha-{sha12}", f"ctx-{ctx_sha64[:12]}"])

        pr_ctx = json.loads(
            self._run_cli(
                "tag-plan",
                "--channel",
                "pr",
                "--git-sha",
                sha_full,
                "--pr-number",
                "42",
                "--context-sha",
                ctx_sha64,
            ).stdout
        )
        self.assertEqual(pr_ctx["context_tag"], f"ctx-{ctx_sha64[:12]}")
        self.assertEqual(pr_ctx["tags"], [f"pr-42-{sha12}"])

        for plan in (local, pr_default, pr_push, main_plan, rel, main_ctx, pr_ctx):
            self.assertNotIn("latest", plan["tags"])
            self.assertFalse(any(ref.endswith(":latest") for ref in plan["references"]))

        # Invalid inputs rejected with exit 2 and no traceback.
        for bad_args in (
            ("tag-plan", "--channel", "main", "--git-sha", "not-a-sha"),
            ("tag-plan", "--channel", "main", "--git-sha", sha_full, "--context-sha", "bad-ctx"),
            ("tag-plan", "--channel", "release", "--git-sha", sha_full, "--version", "latest"),
            ("tag-plan", "--channel", "release", "--git-sha", sha_full, "--version", "v1.2.3"),
            ("tag-plan", "--channel", "pr", "--git-sha", sha_full, "--allow-pr-push"),
            ("tag-plan", "--channel", "local", "--git-sha", sha_full, "--version", "1.0.0"),
            ("tag-plan", "--channel", "invalid", "--git-sha", sha_full),
        ):
            with self.subTest(bad_args=bad_args):
                proc = self._run_cli(*bad_args, expected_rc=2)
                self.assertNotIn("Traceback", proc.stderr)

    def test_plan_gc_retention_gitops_pins_shared_digest_safety_and_strict_json(self) -> None:
        gitops = self.root / "gitops"
        gitops.mkdir()
        d_gitops = "sha256:" + "1" * 64
        d_shared = "sha256:" + "2" * 64
        d_main_new = "sha256:" + "3" * 64
        d_main_old = "sha256:" + "4" * 64
        d_pr_fresh = "sha256:" + "5" * 64
        d_pr_stale = "sha256:" + "6" * 64
        d_dangling = "sha256:" + "7" * 64
        d_explicit = "sha256:" + "8" * 64
        d_ctx_orphan = "sha256:" + "9" * 64

        (gitops / "prod.yaml").write_text(
            f"image: ghcr.io/nestharus/managed-ai-information@{d_gitops}\n",
            encoding="utf-8",
        )

        catalog = self.root / "catalog.json"
        _write_json(
            catalog,
            {
                "format": image_lifecycle.CATALOG_FORMAT,
                "repository": "ghcr.io/nestharus/managed-ai-information",
                "manifests": [
                    {
                        "digest": d_gitops,
                        "created_at": "2026-01-01T00:00:00Z",
                        "tags": ["sha-000000000001"],
                    },
                    {
                        "digest": d_shared,
                        "created_at": "2026-10-01T00:00:00Z",
                        "tags": ["v1.0.0", "pr-9-000000000002"],
                    },
                    {
                        "digest": d_main_new,
                        "created_at": "2026-11-25T00:00:00Z",
                        "tags": ["sha-000000000003", "ctx-aaaaaaaaaaaa"],
                    },
                    {
                        "digest": d_main_old,
                        "created_at": "2026-11-01T00:00:00Z",
                        "tags": ["sha-000000000004", "ctx-bbbbbbbbbbbb"],
                    },
                    {
                        "digest": d_pr_fresh,
                        "created_at": "2026-11-24T00:00:00Z",
                        "tags": ["pr-10-000000000005", "ctx-cccccccccccc"],
                    },
                    {
                        "digest": d_pr_stale,
                        "created_at": "2026-11-05T00:00:00Z",
                        "tags": ["pr-8-000000000006"],
                    },
                    {"digest": d_dangling, "created_at": "2026-11-02T00:00:00Z", "tags": []},
                    {"digest": d_explicit, "created_at": "2026-02-01T00:00:00Z", "tags": []},
                    {
                        "digest": d_ctx_orphan,
                        "created_at": "2026-10-15T00:00:00Z",
                        "tags": ["ctx-dddddddddddd"],
                    },
                ],
            },
        )

        plan = json.loads(
            self._run_cli(
                "plan-gc",
                "--catalog",
                str(catalog),
                "--gitops-dir",
                str(gitops),
                "--protected-digest",
                d_explicit,
                "--keep-main",
                "1",
                "--pr-ttl-days",
                "7",
                "--now",
                "2026-11-26T00:00:00Z",
            ).stdout
        )

        self.assertTrue(plan["dry_run"])
        kept = {row["digest"]: row for row in plan["keep_manifests"]}
        pruned = {row["digest"]: row for row in plan["prune_manifests"]}

        self.assertEqual(set(kept), {d_gitops, d_shared, d_main_new, d_pr_fresh, d_explicit})
        self.assertEqual(set(pruned), {d_main_old, d_pr_stale, d_dangling, d_ctx_orphan})
        self.assertIn("unreferenced-dangling-manifest", pruned[d_dangling]["reasons"])

        # Shared-digest & ctx-* safety:
        # - ctx-aaaaaaaaaaaa on newest main is KEPT (not in untag_only)
        # - ctx-bbbbbbbbbbbb on old main & ctx-dddddddddddd orphan are PRUNED
        # - ctx-cccccccccccc on PR-only digest is untag_only
        # - expired PR tag on d_shared & old sha-* tag on d_gitops go to untag_only, NEVER prune!
        untag_pairs = {(row["digest"], row["tag"]) for row in plan["untag_only"]}
        self.assertNotIn((d_main_new, "ctx-aaaaaaaaaaaa"), untag_pairs)
        self.assertIn((d_pr_fresh, "ctx-cccccccccccc"), untag_pairs)
        self.assertIn((d_shared, "pr-9-000000000002"), untag_pairs)
        self.assertIn((d_gitops, "sha-000000000001"), untag_pairs)
        self.assertNotIn(d_shared, pruned)
        self.assertNotIn(d_gitops, pruned)

        # Strict JSON: duplicate keys and NaN rejected with exit 2.
        dup_cat = self.root / "dup.json"
        dup_cat.write_text('{"format": "a", "format": "b"}\n', encoding="utf-8")
        proc_dup = self._run_cli("plan-gc", "--catalog", str(dup_cat), expected_rc=2)
        self.assertNotIn("Traceback", proc_dup.stderr)

        nan_cat = self.root / "nan.json"
        nan_cat.write_text(
            '{"format": "managed-ai-registry-catalog/1", "x": NaN}\n', encoding="utf-8"
        )
        proc_nan = self._run_cli("plan-gc", "--catalog", str(nan_cat), expected_rc=2)
        self.assertNotIn("Traceback", proc_nan.stderr)

        # Symlink / junction gitops dir rejected with exit 2.
        linked_gitops = self.root / "linked-gitops"
        self._make_dir_link(linked_gitops, gitops)
        proc_link = self._run_cli(
            "plan-gc", "--catalog", str(catalog), "--gitops-dir", str(linked_gitops), expected_rc=2
        )
        self.assertNotIn("Traceback", proc_link.stderr)

    def test_plan_gc_protects_kustomize_digest_pins_and_live_repo_gitops(self) -> None:
        gitops = self.root / "kustomize-gitops"
        (gitops / "base").mkdir(parents=True)
        (gitops / "channels/dev").mkdir(parents=True)
        (gitops / "channels/local").mkdir(parents=True)

        d_base = "sha256:" + "a" * 64
        d_dev = "sha256:" + "b" * 64
        d_local = "sha256:" + "c" * 64
        d_unpinned = "sha256:" + "d" * 64

        (gitops / "base/deployment.yaml").write_text(
            f"image: ghcr.io/nestharus/managed-ai-information@{d_base}\n",
            encoding="utf-8",
        )
        (gitops / "channels/dev/kustomization.yaml").write_text(
            "\n".join(
                [
                    "apiVersion: kustomize.config.k8s.io/v1beta1",
                    "kind: Kustomization",
                    "images:",
                    "  - name: ghcr.io/nestharus/managed-ai-information",
                    f"    digest: {d_dev}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (gitops / "channels/local/kustomization.yaml").write_text(
            "\n".join(
                [
                    "apiVersion: kustomize.config.k8s.io/v1beta1",
                    "kind: Kustomization",
                    "images:",
                    "  - name: ghcr.io/nestharus/managed-ai-information",
                    f'    digest: "{d_local}"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        catalog = self.root / "kustomize-catalog.json"
        _write_json(
            catalog,
            {
                "format": image_lifecycle.CATALOG_FORMAT,
                "repository": "ghcr.io/nestharus/managed-ai-information",
                "manifests": [
                    {
                        "digest": d_base,
                        "created_at": "2026-01-01T00:00:00Z",
                        "tags": ["sha-00000000000a"],
                    },
                    {
                        "digest": d_dev,
                        "created_at": "2026-01-02T00:00:00Z",
                        "tags": ["sha-00000000000b"],
                    },
                    {
                        "digest": d_local,
                        "created_at": "2026-01-03T00:00:00Z",
                        "tags": ["sha-00000000000c"],
                    },
                    {
                        "digest": d_unpinned,
                        "created_at": "2026-01-04T00:00:00Z",
                        "tags": ["sha-00000000000d"],
                    },
                    {
                        "digest": "sha256:" + "e" * 64,
                        "created_at": "2026-11-25T00:00:00Z",
                        "tags": ["sha-00000000000e"],
                    },
                ],
            },
        )

        plan = json.loads(
            self._run_cli(
                "plan-gc",
                "--catalog",
                str(catalog),
                "--gitops-dir",
                str(gitops),
                "--keep-main",
                "1",
                "--now",
                "2026-11-26T00:00:00Z",
            ).stdout
        )

        self.assertEqual(
            plan["protected_gitops_digests"],
            {
                d_base: ["base/deployment.yaml"],
                d_dev: ["channels/dev/kustomization.yaml"],
                d_local: ["channels/local/kustomization.yaml"],
            },
        )
        kept = {row["digest"] for row in plan["keep_manifests"]}
        pruned = {row["digest"] for row in plan["prune_manifests"]}
        self.assertTrue({d_base, d_dev, d_local}.issubset(kept))
        self.assertEqual(pruned, {d_unpinned})

        # Verify against the live repository gitops/ directory: all 3 manifests are collected.
        live_pins = image_lifecycle._collect_gitops_pins(REPO / "gitops")
        live_sources = sorted({src for sources in live_pins.values() for src in sources})
        self.assertEqual(
            live_sources,
            [
                "base/deployment.yaml",
                "channels/dev/kustomization.yaml",
                "channels/local/kustomization.yaml",
            ],
        )

    def test_buildkite_pipelines_structure_gates_and_hygiene(self) -> None:
        for path in (PIPELINE, NIGHTLY):
            raw = path.read_bytes()
            self.assertNotIn(b"\r\n", raw, f"CRLF found in {path}")
            text = raw.decode("utf-8")
            self.assertNotIn(":latest", text)
            self.assertIn("PYTHONDONTWRITEBYTECODE", text)

        main_yaml = PIPELINE.read_text(encoding="utf-8")
        wait_pos = main_yaml.index("- wait")
        for gate in (
            (
                "uvx --from ruff==0.17.6 ruff check src scripts tests"
                " && uvx --from ruff==0.17.6 ruff format --check src scripts tests"
            ),
            (
                "uvx --from ty==0.0.112 ty check src scripts"
                " && uvx --from mypy==2.4.1 mypy src scripts"
            ),
            "python3 -B -m unittest discover -s tests",
            "python3 -B scripts/release/check_current.py",
            "python3 -B scripts/ci/validate_deploy.py",
            "python3 -B -m unittest tests/release/test_package_wheel.py",
        ):
            self.assertIn(gate, main_yaml)
            self.assertLess(main_yaml.index(gate), wait_pos, f"Gate {gate} must run before - wait")
        for post_gate in (
            "scripts/ci/image_lifecycle.py context-hash",
            "scripts/ci/image_lifecycle.py tag-plan",
            "--context-sha",
            "crane tag",
            '"${TAG_FLAGS[@]}"',
            '"${PR_ARGS[@]}"',
            "containers/Containerfile",
        ):
            self.assertIn(post_gate, main_yaml)
            self.assertGreater(
                main_yaml.index(post_gate), wait_pos, f"{post_gate} must run after - wait"
            )
        self.assertNotIn(" $TAG_FLAGS", main_yaml)
        self.assertNotIn(" $PR_ARGS", main_yaml)

        nightly_yaml = NIGHTLY.read_text(encoding="utf-8")
        self.assertIn("scripts/ci/image_lifecycle.py plan-gc", nightly_yaml)
        self.assertIn("--gitops-dir gitops", nightly_yaml)
        self.assertIn("pack-objects --apply", nightly_yaml)

        # AST & hygiene checks on scripts/ci/image_lifecycle.py
        src = SCRIPT.read_text(encoding="utf-8")
        semis = [
            tok.start[0]
            for tok in tokenize.generate_tokens(io.StringIO(src).readline)
            if tok.type == tokenize.OP and tok.string == ";"
        ]
        self.assertEqual(semis, [])
        tree = ast.parse(src, filename=str(SCRIPT))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                span = (node.end_lineno or node.lineno) - node.lineno + 1
                self.assertLessEqual(span, 120, f"{node.name} is {span}L > 120L")
            if isinstance(node, ast.Call):
                fn = node.func
                name = (
                    fn.attr
                    if isinstance(fn, ast.Attribute)
                    else (fn.id if isinstance(fn, ast.Name) else None)
                )
                if name in ("ArgumentParser", "add_parser"):
                    ok = any(
                        kw.arg == "allow_abbrev"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is False
                        for kw in node.keywords
                    )
                    self.assertTrue(
                        ok, f"Parser call at line {node.lineno} missing allow_abbrev=False"
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
