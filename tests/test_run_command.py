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
    def test_run_imports_a_task_file_into_the_real_kernel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "target"
            task_path = temp_path / "task.json"
            data_dir = temp_path / "workflow-data"
            repository.mkdir()
            subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "agentflow@example.test"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Agentflow Test"],
                cwd=repository,
                check=True,
            )
            (repository / "README.md").write_text("# Target\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial commit"],
                cwd=repository,
                check=True,
                capture_output=True,
            )
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
                cwd=repository,
                env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual(response["state"], "ready")
            self.assertTrue(response["run_id"])
            self.assertTrue((data_dir / "runs" / response["run_id"] / "events.jsonl").is_file())
            self.assertTrue(Path(response["worktree"]).is_dir())
            self.assertEqual(
                json.loads(
                    (data_dir / "runs" / response["run_id"] / "task.json").read_text(
                        encoding="utf-8"
                    )
                ),
                {
                    "acceptance_criteria": [],
                    "summary": "Add a health endpoint",
                },
            )

    def test_run_preserves_source_and_criteria_and_rejects_unknown_fields(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "target"
            task_path = temp_path / "task.json"
            bad_task_path = temp_path / "bad-task.json"
            data_dir = temp_path / "workflow-data"
            repository.mkdir()
            subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "agentflow@example.test"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Agentflow Test"],
                cwd=repository,
                check=True,
            )
            (repository / "README.md").write_text("# Target\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial commit"],
                cwd=repository,
                check=True,
                capture_output=True,
            )
            source = {
                "provider": "github",
                "work_item_id": "99",
                "captured_at": "2026-07-15T12:00:00Z",
                "content_hash": "c" * 64,
            }
            task_path.write_text(
                json.dumps(
                    {
                        "summary": "Add a health endpoint",
                        "acceptance_criteria": ["checks pass"],
                        "source": source,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            bad_task_path.write_text(
                json.dumps(
                    {"summary": "Add a health endpoint", "unexpected": 1}
                )
                + "\n",
                encoding="utf-8",
            )

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
                cwd=repository,
                env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            run_id = json.loads(result.stdout)["run_id"]
            self.assertEqual(
                json.loads(
                    (data_dir / "runs" / run_id / "task.json").read_text(
                        encoding="utf-8"
                    )
                ),
                {
                    "acceptance_criteria": ["checks pass"],
                    "source": source,
                    "summary": "Add a health endpoint",
                },
            )

            rejected = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentflow",
                    "run",
                    str(bad_task_path),
                    "--data-dir",
                    str(data_dir),
                ],
                cwd=repository,
                env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("unknown fields", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
