# Copyright (C) 2026 embeff GmbH
# SPDX-License-Identifier: Apache-2.0
# This file is part of the ExecutionPlatform distribution. More information at https://embeff.com

"""How users reach the package: the Robot library EPLibrary, the pytest11 plugin and the ep-config script."""

import importlib.metadata
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def installed():
    try:
        importlib.metadata.distribution("embeff-ep")
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


class TestRobotLibrary(unittest.TestCase):
    def test_library_by_name(self):
        from robot.utils import Importer

        library = Importer().import_class_or_module("EPLibrary")
        self.assertEqual((library.__name__, library.__module__), ("EPLibrary", "embeff_ep.robot"))


@unittest.skipUnless(installed(), "embeff-ep is not installed in this environment")
class TestInstalled(unittest.TestCase):
    def test_pytest_plugin_entry_point(self):
        plugins = importlib.metadata.entry_points(group="pytest11")
        self.assertEqual([ep.value for ep in plugins if ep.name == "embeff_ep"], ["embeff_ep.pytest_plugin"])

    def test_plugin_is_inert_without_an_ep(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "test_nothing.py").write_text("def test_nothing():\n    pass\n")
            env = {k: v for k, v in os.environ.items() if k != "PYTEST_DISABLE_PLUGIN_AUTOLOAD"}
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", tmp],
                capture_output=True,
                text=True,
                env=env,
                cwd=tmp,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("embeff-ep", result.stdout)  # the "plugins:" line of the run header

    def test_ep_config_script(self):
        script = Path(sys.executable).parent / ("ep-config.exe" if os.name == "nt" else "ep-config")
        result = subprocess.run([str(script), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: ep-config", result.stdout)


if __name__ == "__main__":
    unittest.main()
