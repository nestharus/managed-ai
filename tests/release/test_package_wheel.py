"""Verify the PEP-517 wheel distribution built from root pyproject.toml and src/managed_ai."""

import json
import shutil
import subprocess
import sys
import unittest
import zipfile
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[2]


class PackageWheelTest(unittest.TestCase):
    @unittest.skipUnless(
        find_spec("setuptools") and find_spec("wheel"), "requires the wheel build tools"
    )
    def test_wheel_version_package_data_console_targets_and_no_maintainer_scripts(self):
        with TemporaryDirectory() as tmp:
            build = Path(tmp) / "managed-ai-build"
            build.mkdir()
            for filename in ("pyproject.toml", "LICENSE", "README.md"):
                shutil.copy2(REPO / filename, build / filename)
            shutil.copytree(
                REPO / "src",
                build / "src",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    "from setuptools.build_meta import build_wheel; build_wheel('.')",
                ],
                cwd=build,
                check=True,
                capture_output=True,
            )
            (wheel,) = build.glob("*.whl")

            with zipfile.ZipFile(wheel) as archive:
                names = set(archive.namelist())

            # Maintainer scripts, retired shims, and release assets must NEVER ship inside the
            # wheel.
            for forbidden in (
                "managed_ai/release/__init__.py",
                "managed_ai/release/current_sources.json",
                "managed_ai/release/navigation.json",
                "managed_ai/release/package.py",
                "managed_ai/release/check_current.py",
                "managed_ai/release/refresh_guidance.py",
                "managed_ai/library/bundles/publish.py",
                "managed_ai/library/bundles/product_bundle.py",
                "managed_ai/library/reader/knowledge.py",
                "managed_ai/library/search/lexical_search.py",
                "managed_ai/core/cli/exchange_packets.py",
                "managed_ai/tools/audit_feedback.py",
                "managed_ai/tools/open_contact.py",
            ):
                self.assertNotIn(forbidden, names)
            self.assertFalse(any(name.startswith("managed_ai/release/") for name in names))

            # Runtime data assets must still be bundled cleanly inside the wheel.
            for required in (
                "managed_ai/CURRENT-RELEASE.json",
                "managed_ai/core/NOTICE.md",
                "managed_ai/store/GUIDE.md",
                "managed_ai/store/catalogue.json",
                "managed_ai/library/source-map.json",
                "managed_ai/store/governance/audit_feedback.py",
                "managed_ai/core/exchange/open_contact.py",
            ):
                self.assertIn(required, names)

            probe = """
import json, sys
sys.path.insert(0, sys.argv[1])
import managed_ai
import managed_ai.platform
from importlib.metadata import distribution
dist = distribution('managed-ai')
commands = [
    ep.name
    for ep in dist.entry_points
    if ep.group == 'console_scripts' and callable(ep.load())
]
print(json.dumps([dist.version, managed_ai.__version__, sorted(commands)]))
"""
            raw = subprocess.check_output(
                [sys.executable, "-I", "-c", probe, str(wheel)], cwd=build
            )
            metadata, imported, commands = json.loads(raw)
            self.assertEqual(metadata, imported)
            self.assertEqual(wheel.name, f"managed_ai-{imported}-py3-none-any.whl")
            self.assertEqual(
                commands,
                [
                    "g60-attend",
                    "g60-collect",
                    "g60-work",
                    "managed-ai",
                    "managed-ai-deploy",
                    "managed-ai-library",
                ],
            )


if __name__ == "__main__":
    unittest.main()
