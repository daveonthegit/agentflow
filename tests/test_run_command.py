from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).parents[1]


class RunCommandTests(unittest.TestCase):
    def test_run_creates_a_run_and_stops_for_human_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            task_path = temp_path / "task.json"
            data_dir = temp_path / "workflow-data"
            task_path.write_text('{"summary": "Add a health endpoint"}\n', encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentflow",
                    "run",
                    str(task_path),
                    "--data-dir",
                    str(data_dir),
                ],
                cwd=PROJECT_ROOT,
                env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual(response["state"], "awaiting_human")
            self.assertTrue(response["run_id"])
            self.assertTrue((data_dir / "runs" / response["run_id"] / "events.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
