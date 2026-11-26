"""The release gate runs the requested engine even under a cross-product test environment."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[2]
CHECK_SCRIPT = REPO / "scripts/release/check_current.py"


class ReleaseAcceptance(unittest.TestCase):
    def test_explicit_product_supplies_the_imported_implementation(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate_repo = root / "candidate"
            inherited_repo = root / "inherited"
            for repo, label in ((candidate_repo, "candidate"), (inherited_repo, "inherited")):
                pkg = repo / "src/managed_ai"
                store = pkg / "store"
                store.mkdir(parents=True)
                (pkg / "__init__.py").write_text("", encoding="utf-8")
                (store / "__init__.py").write_text("", encoding="utf-8")
                (store / "edition.py").write_text(f"NAME = {label!r}\n", encoding="utf-8")
            suites = candidate_repo / "tests/store"
            suites.mkdir(parents=True)
            program = suites / "test_shared_instances.py"
            program.write_text(
                "import os, sys\n"
                "from pathlib import Path\n"
                'sys.path.insert(0, str(Path(os.environ["G60_TEST_PRODUCT"]).parent))\n'
                "from managed_ai.store.edition import NAME\n"
                "print(NAME)\n",
                encoding="utf-8",
            )
            env = dict(os.environ, G60_TEST_PRODUCT=str(inherited_repo / "src/managed_ai"))
            standalone = subprocess.check_output(
                [sys.executable, "-B", str(program)], env=env, text=True
            )
            self.assertEqual(standalone.strip(), "inherited")
            subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(CHECK_SCRIPT),
                    "--product",
                    str(candidate_repo / "src/managed_ai"),
                    "--records",
                    str(root),
                ],
                env=env,
                check=True,
                capture_output=True,
            )
            report = json.loads((root / "ACCEPTANCE.json").read_bytes())
            self.assertEqual(report["stdout"].strip(), "candidate")
            self.assertEqual(report["program"], str(program))
            self.assertEqual(report["product"], str(candidate_repo / "src/managed_ai"))

    def test_w5_b8b_rejects_junction_or_symlink_records_and_abbreviated_flags(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            (repo / "src/managed_ai").mkdir(parents=True)
            suites = repo / "tests/store"
            suites.mkdir(parents=True)
            (suites / "test_shared_instances.py").write_text('print("ok")\n', encoding="utf-8")
            real_rec = root / "real_records"
            real_rec.mkdir()
            alias_rec = root / "alias_records"
            if os.name == "nt":
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(alias_rec), str(real_rec)],
                    check=True,
                    capture_output=True,
                )
            else:
                alias_rec.symlink_to(real_rec)

            # W5-B8b: --records pointing at an NTFS junction / POSIX symlink must be refused before
            # writing
            res = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(CHECK_SCRIPT),
                    "--product",
                    str(repo / "src/managed_ai"),
                    "--records",
                    str(alias_rec),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("ordinary directory required", res.stderr)
            self.assertFalse((real_rec / "ACCEPTANCE.json").exists())

            # Raw .. traversing through a junction/symlink must also be refused before lexical
            # collapse
            res_dotdot = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(CHECK_SCRIPT),
                    "--product",
                    str(repo / "src/managed_ai"),
                    "--records",
                    str(alias_rec) + os.sep + ".." + os.sep + "real_records",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res_dotdot.returncode, 0)
            self.assertIn("ordinary directory required", res_dotdot.stderr)
            self.assertFalse((real_rec / "ACCEPTANCE.json").exists())

            # W5-B11: allow_abbrev=False rejects abbreviated --rec
            abbrev = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(CHECK_SCRIPT),
                    "--product",
                    str(repo / "src/managed_ai"),
                    "--rec",
                    str(real_rec),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(abbrev.returncode, 0)
            self.assertFalse((real_rec / "ACCEPTANCE.json").exists())

    def test_c41_1_ignores_stale_or_nonexistent_posix_pwd(self):
        import runpy

        mod = runpy.run_path(str(CHECK_SCRIPT))
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            dir_a = root / "dir_a"
            dir_b = root / "dir_b"
            (dir_a / "sub").mkdir(parents=True)
            (dir_b / "sub").mkdir(parents=True)
            old_cwd = os.getcwd()
            old_pwd = os.environ.get("PWD")
            try:
                os.chdir(dir_b)
                os.environ["PWD"] = str(dir_a)
                got = mod["_require_ordinary_ancestors"](Path("sub"), "test")
                self.assertEqual(got.resolve(), (dir_b / "sub").resolve())

                os.environ["PWD"] = str(root / "does_not_exist")
                got2 = mod["_require_ordinary_ancestors"](Path("sub"), "test")
                self.assertEqual(got2.resolve(), (dir_b / "sub").resolve())

                if os.name != "nt":
                    link_b = root / "link_b"
                    link_b.symlink_to(dir_b)
                    os.environ["PWD"] = str(link_b)
                    with self.assertRaisesRegex(ValueError, "ordinary directory required"):
                        mod["_require_ordinary_ancestors"](Path("sub"), "test")
            finally:
                os.chdir(old_cwd)
                if old_pwd is None:
                    os.environ.pop("PWD", None)
                else:
                    os.environ["PWD"] = old_pwd


if __name__ == "__main__":
    unittest.main()
