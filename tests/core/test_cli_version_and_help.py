"""Regression tests for DX12-3: --version on all 6 CLIs and hidden collector _worker."""

import subprocess
import sys
import unittest
from pathlib import Path

from tests._paths import REPO

# isort: split
from managed_ai_information import __version__
from managed_ai_information.core.exchange.transport.collector import (
    _build_parser as build_collector_parser,
)
from managed_ai_information.library.config import LibraryConfig
from managed_ai_information.platform._common import admission_deadline

_CLI_SCRIPTS: tuple[tuple[str, Path, tuple[str, ...]], ...] = (
    (
        "managed-ai-information",
        REPO / "src/managed_ai_information/store/manage_store.py",
        ("--version",),
    ),
    (
        "managed-ai-information-deploy",
        REPO / "src/managed_ai_information/deployment/select_deployment.py",
        ("--version",),
    ),
    (
        "managed-ai-information-library",
        REPO / "src/managed_ai_information/library/cli.py",
        ("--version",),
    ),
    (
        "managed-ai-information-library-published",
        REPO / "src/managed_ai_information/library/cli.py",
        ("--library", ".", "--version"),
    ),
    (
        "managed-ai-information-attend",
        REPO / "src/managed_ai_information/core/attention.py",
        ("--version",),
    ),
    (
        "managed-ai-information-work",
        REPO / "src/managed_ai_information/core/exchange/cli.py",
        ("--version",),
    ),
    (
        "managed-ai-information-collect",
        REPO / "src/managed_ai_information/core/exchange/transport/collector.py",
        ("--version",),
    ),
)


class CliVersionAndHelpTests(unittest.TestCase):
    def test_all_six_clis_support_version_flag(self) -> None:
        """DX12-3: every public CLI prints __version__ and exits 0 on --version."""
        for label, script, extra_args in _CLI_SCRIPTS:
            with self.subTest(cli=label):
                proc = subprocess.run(
                    [sys.executable, "-B", str(script), *extra_args],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=30,
                )
                self.assertEqual(
                    proc.returncode,
                    0,
                    f"{label} --version failed: rc={proc.returncode} stderr={proc.stderr!r}",
                )
                self.assertIn(__version__, proc.stdout)

    def test_collector_help_hides_private_worker_but_keeps_it_invocable(self) -> None:
        """DX12-3: collector --help omits _worker while parser still accepts internal _worker."""
        collector = REPO / "src/managed_ai_information/core/exchange/transport/collector.py"
        proc = subprocess.run(
            [sys.executable, "-B", str(collector), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("_worker", proc.stdout)
        self.assertIn("{admit,resume,list,receive}", proc.stdout)

        parsed = build_collector_parser().parse_args(
            ["--root", "dummy", "_worker", "a" * 64, "tok-1"]
        )
        self.assertEqual(parsed.command, "_worker")
        self.assertEqual(parsed.key, "a" * 64)
        self.assertEqual(parsed.token, "tok-1")

    def test_env_vars_prefer_new_prefix_with_legacy_fallback(self) -> None:
        """Wave 13: MANAGED_AI_INFORMATION_* takes precedence with MANAGED_AI_* fallback."""
        from unittest.mock import patch

        cfg_primary = LibraryConfig.from_env(
            {
                "MANAGED_AI_INFORMATION_LIBRARY_MAX_RETAINED_CATALOGUES": "7",
                "MANAGED_AI_LIBRARY_MAX_RETAINED_CATALOGUES": "3",
            }
        )
        self.assertEqual(cfg_primary.search.max_retained_catalogues, 7)

        cfg_legacy = LibraryConfig.from_env({"MANAGED_AI_LIBRARY_MAX_RETAINED_CATALOGUES": "4"})
        self.assertEqual(cfg_legacy.search.max_retained_catalogues, 4)

        with patch.dict(
            "os.environ",
            {"MANAGED_AI_INFORMATION_DOMAIN_ADMISSION_SECONDS": "2"},
            clear=False,
        ):
            self.assertIsNotNone(admission_deadline())


if __name__ == "__main__":
    unittest.main()
