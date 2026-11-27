"""Dual-OS static validation tests for Containerfile, compose, OpenTofu, and GitOps."""

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.ci.validate_deploy import validate_repository  # noqa: E402

from managed_ai_information import __version__ as PKG_VERSION  # noqa: E402

DEPLOY_PATHS = (
    "pyproject.toml",
    ".dockerignore",
    ".sops.yaml",
    "containers",
    "deploy",
    "infra",
    "gitops",
)


def _copy_deploy_tree(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    for rel in DEPLOY_PATHS:
        src = _REPO / rel
        dst = dest / rel
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    (dest / "src" / "managed_ai_information").mkdir(parents=True, exist_ok=True)
    return dest


class DeployStaticValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="managed-ai-w10-deploy-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp_root = Path(self._tmp.name)

    def test_repository_deploy_substrate_passes_and_cli_exits_zero(self) -> None:
        report = validate_repository(_REPO)
        self.assertEqual(report["status"], "passed", report["errors"])
        self.assertEqual(report["error_count"], 0)

        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(_REPO / "scripts/ci/validate_deploy.py"),
                "--root",
                str(_REPO),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["error_count"], 0)

    def test_cli_rejects_missing_root_with_exit_2_json_stderr(self) -> None:
        missing = self.tmp_root / "does-not-exist"
        self.assertTrue(bool(PKG_VERSION))
        with self.assertRaises(ValueError):
            validate_repository(missing)
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(_REPO / "scripts/ci/validate_deploy.py"),
                "--root",
                str(missing),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(proc.returncode, 2)
        payload = json.loads(proc.stderr)
        self.assertEqual(payload["status"], "error")

    def test_rejects_latest_tag_in_containerfile_and_gitops(self) -> None:
        repo = _copy_deploy_tree(self.tmp_root / "bad-latest")
        cf = repo / "containers/Containerfile"
        cf.write_text(
            cf.read_text(encoding="utf-8").replace("python:3.15.2-slim", "python:latest"),
            encoding="utf-8",
        )

        report = validate_repository(repo)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any(":latest" in err for err in report["errors"]), report["errors"])

        repo2 = _copy_deploy_tree(self.tmp_root / "bad-gitops-latest")
        dep = repo2 / "gitops/base/deployment.yaml"
        dep.write_text(
            dep.read_text(encoding="utf-8").replace(
                "@sha256:0000000000000000000000000000000000000000000000000000000000000000",
                ":latest",
            ),
            encoding="utf-8",
        )
        report2 = validate_repository(repo2)
        self.assertEqual(report2["status"], "failed")
        self.assertTrue(any(":latest" in err for err in report2["errors"]), report2["errors"])

    def test_rejects_missing_user_in_containerfile(self) -> None:
        repo = _copy_deploy_tree(self.tmp_root / "bad-user")
        cf = repo / "containers/Containerfile"
        lines = [
            ln for ln in cf.read_text(encoding="utf-8").splitlines() if not ln.startswith("USER ")
        ]
        cf.write_text("\n".join(lines) + "\n", encoding="utf-8")

        report = validate_repository(repo)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any("USER" in err for err in report["errors"]), report["errors"])

    def test_rejects_missing_internal_network_and_wildcard_host_bind_in_compose(self) -> None:
        repo = _copy_deploy_tree(self.tmp_root / "bad-internal")
        compose = repo / "deploy/local/compose.yaml"
        compose.write_text(
            compose.read_text(encoding="utf-8").replace("internal: true", "internal: false"),
            encoding="utf-8",
        )

        report = validate_repository(repo)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any("internal: true" in err for err in report["errors"]), report["errors"])

        repo2 = _copy_deploy_tree(self.tmp_root / "bad-bind")
        compose2 = repo2 / "deploy/local/compose.yaml"
        compose2.write_text(
            compose2.read_text(encoding="utf-8").replace("127.0.0.1:", "0.0.0.0:"),
            encoding="utf-8",
        )
        report2 = validate_repository(repo2)
        self.assertEqual(report2["status"], "failed")
        self.assertTrue(
            any("0.0.0.0" in err or "127.0.0.1" in err for err in report2["errors"]),  # noqa: S104
            report2["errors"],
        )

    def test_rejects_missing_networkpolicy_default_deny(self) -> None:
        repo = _copy_deploy_tree(self.tmp_root / "bad-netpol")
        netpol = repo / "gitops/base/networkpolicy.yaml"
        netpol.write_text(
            netpol.read_text(encoding="utf-8").replace(
                "podSelector: {}",
                "\n".join(["podSelector:", "    matchLabels:", "      app: x"]),
            ),
            encoding="utf-8",
        )
        report = validate_repository(repo)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any("default-deny" in err for err in report["errors"]), report["errors"])

    def test_rejects_hardcoded_secret_and_public_ip_in_infra(self) -> None:
        repo = _copy_deploy_tree(self.tmp_root / "bad-secret")
        dev = repo / "infra/channels/dev.yaml"
        dev.write_text(
            dev.read_text(encoding="utf-8")
            + "\nsecret_key: AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQ\n",
            encoding="utf-8",
        )
        report = validate_repository(repo)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any("secret" in err.lower() for err in report["errors"]), report["errors"])

        repo2 = _copy_deploy_tree(self.tmp_root / "bad-public-ip")
        dev2 = repo2 / "infra/channels/dev.yaml"
        dev2.write_text(
            dev2.read_text(encoding="utf-8").replace(
                "admin_cidrs: []", 'admin_cidrs: ["203.0.113.10/32"]'
            ),
            encoding="utf-8",
        )
        report2 = validate_repository(repo2)
        self.assertEqual(report2["status"], "failed")
        self.assertTrue(any("203.0.113.10" in err for err in report2["errors"]), report2["errors"])

    def test_rejects_wheel_package_leak(self) -> None:
        repo = _copy_deploy_tree(self.tmp_root / "bad-wheel")
        (repo / "src" / "managed_ai_information" / "gitops").mkdir(parents=True)
        report = validate_repository(repo)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(
            any("forbidden deploy/CI directory" in err for err in report["errors"]),
            report["errors"],
        )

    def test_rejects_intermediate_dir_symlink_or_junction(self) -> None:
        repo = _copy_deploy_tree(self.tmp_root / "bad-link-dir")
        external = self.tmp_root / "external-gitops"
        (repo / "gitops").rename(external)
        link = repo / "gitops"
        if os.name == "nt":
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(external)], capture_output=True
            )
            self.assertEqual(proc.returncode, 0)
            self.addCleanup(
                lambda: subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)
            )
        else:
            link.symlink_to(external)

        report = validate_repository(repo)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(
            any("ordinary non-symlink file required" in err for err in report["errors"]),
            report["errors"],
        )


if __name__ == "__main__":
    unittest.main()
