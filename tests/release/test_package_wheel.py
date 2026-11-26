"""Verify the PEP-517 wheel distribution built from root pyproject.toml."""

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
                "managed_ai_information/release/__init__.py",
                "managed_ai_information/release/current_sources.json",
                "managed_ai_information/release/navigation.json",
                "managed_ai_information/release/package.py",
                "managed_ai_information/release/check_current.py",
                "managed_ai_information/release/refresh_guidance.py",
                "managed_ai_information/library/bundles/publish.py",
                "managed_ai_information/library/bundles/product_bundle.py",
                "managed_ai_information/library/reader/knowledge.py",
                "managed_ai_information/library/search/lexical_search.py",
                "managed_ai_information/core/cli/exchange_packets.py",
                "managed_ai_information/tools/audit_feedback.py",
                "managed_ai_information/tools/open_contact.py",
            ):
                self.assertNotIn(forbidden, names)
            self.assertFalse(
                any(name.startswith("managed_ai_information/release/") for name in names)
            )

            # Runtime data assets must still be bundled cleanly inside the wheel.
            for required in (
                "managed_ai_information/py.typed",
                "managed_ai_information/CURRENT-RELEASE.json",
                "managed_ai_information/core/NOTICE.md",
                "managed_ai_information/store/GUIDE.md",
                "managed_ai_information/store/catalogue.json",
                "managed_ai_information/library/source-map.json",
                "managed_ai_information/store/governance/audit_feedback.py",
                "managed_ai_information/core/exchange/open_contact.py",
            ):
                self.assertIn(required, names)

            probe = """
import json, sys
sys.path.insert(0, sys.argv[1])
import managed_ai_information
import managed_ai_information.platform
from importlib.metadata import distribution
dist = distribution('managed-ai-information')
commands = [
    ep.name
    for ep in dist.entry_points
    if ep.group == 'console_scripts' and callable(ep.load())
]
print(json.dumps([dist.version, managed_ai_information.__version__, sorted(commands)]))
"""
            raw = subprocess.check_output(
                [sys.executable, "-I", "-c", probe, str(wheel)], cwd=build
            )
            metadata, imported, commands = json.loads(raw)
            self.assertEqual(metadata, imported)
            self.assertEqual(wheel.name, f"managed_ai_information-{imported}-py3-none-any.whl")
            self.assertEqual(
                commands,
                [
                    "g60-attend",
                    "g60-collect",
                    "g60-work",
                    "managed-ai",
                    "managed-ai-attend",
                    "managed-ai-collect",
                    "managed-ai-deploy",
                    "managed-ai-information",
                    "managed-ai-information-attend",
                    "managed-ai-information-collect",
                    "managed-ai-information-deploy",
                    "managed-ai-information-library",
                    "managed-ai-information-work",
                    "managed-ai-library",
                    "managed-ai-work",
                ],
            )


if __name__ == "__main__":
    unittest.main()
