from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).parents[1]


class NpxInstallerTests(unittest.TestCase):
    def test_dry_run_plans_full_repository_cli_and_skill_install(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required to exercise the npx installer")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            install_root = temp_path / "agentflow"
            bin_dir = temp_path / "bin"

            result = subprocess.run(
                [
                    node,
                    str(PROJECT_ROOT / "bin" / "agentflow-install.mjs"),
                    "install",
                    "--dry-run",
                ],
                cwd=PROJECT_ROOT,
                env={
                    **os.environ,
                    "AGENTFLOW_INSTALL_ROOT": str(install_root),
                    "AGENTFLOW_BIN_DIR": str(bin_dir),
                },
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(plan["state"], "planned")
            self.assertEqual(Path(plan["source"]), install_root / "source")
            self.assertEqual(Path(plan["command"]), bin_dir / "agentflow")
            self.assertIn("clone_repository", plan["steps"])
            self.assertIn("install_cli", plan["steps"])
            self.assertIn("install_global_skill", plan["steps"])


if __name__ == "__main__":
    unittest.main()
