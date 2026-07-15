from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).parents[1]


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


class StartCommandTests(unittest.TestCase):
    def test_start_rejects_a_dirty_target_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "target"
            data_dir = temp_path / "agentflow-home"
            repository.mkdir()
            git("init", cwd=repository)
            git("config", "user.email", "agentflow@example.test", cwd=repository)
            git("config", "user.name", "Agentflow Test", cwd=repository)
            (repository / "README.md").write_text("# Target\n", encoding="utf-8")
            git("add", "README.md", cwd=repository)
            git("commit", "-m", "Initial commit", cwd=repository)
            (repository / "README.md").write_text("dirty\n", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentflow",
                    "start",
                    "Add a health endpoint",
                    "--data-dir",
                    str(data_dir),
                ],
                cwd=repository,
                env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must be clean", result.stderr)
            self.assertFalse(data_dir.exists())

    def test_start_snapshots_task_and_repository_in_an_isolated_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "target"
            data_dir = temp_path / "agentflow-home"
            repository.mkdir()
            git("init", cwd=repository)
            git("config", "user.email", "agentflow@example.test", cwd=repository)
            git("config", "user.name", "Agentflow Test", cwd=repository)
            (repository / "README.md").write_text("# Target\n", encoding="utf-8")
            git("add", "README.md", cwd=repository)
            git("commit", "-m", "Initial commit", cwd=repository)
            base_sha = git("rev-parse", "HEAD", cwd=repository)

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentflow",
                    "start",
                    "Add a health endpoint",
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
            run_dir = data_dir / "runs" / response["run_id"]
            worktree = Path(response["worktree"])
            self.assertEqual(
                json.loads((run_dir / "task.json").read_text(encoding="utf-8")),
                {"summary": "Add a health endpoint"},
            )
            self.assertEqual(
                json.loads(
                    (run_dir / "repository.json").read_text(encoding="utf-8")
                ),
                {
                    "base_sha": base_sha,
                    "repository": str(repository.resolve()),
                },
            )
            self.assertTrue(worktree.is_dir())
            self.assertEqual(git("rev-parse", "HEAD", cwd=worktree), base_sha)


if __name__ == "__main__":
    unittest.main()
