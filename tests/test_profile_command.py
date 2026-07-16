from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).parents[1]


class ProfileCommandTests(unittest.TestCase):
    def test_profile_writes_target_local_repository_understanding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
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
            (repository / "src").mkdir()
            (repository / "tests").mkdir()
            (repository / "README.md").write_text("# Target\n", encoding="utf-8")
            (repository / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            (repository / "tests" / "test_app.py").write_text(
                "assert True\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial commit"],
                cwd=repository,
                check=True,
                capture_output=True,
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentflow",
                    "profile",
                    "--check",
                    "python3 -m unittest discover -s tests -v",
                ],
                cwd=repository,
                env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual(response["state"], "profile_ready")
            profile_path = repository / ".agentflow" / "repository-profile.json"
            self.assertEqual(Path(response["profile"]), profile_path.resolve())
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertEqual(profile["schema_version"], 1)
            self.assertEqual(
                profile["checks"],
                [["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]],
            )
            self.assertEqual(profile["map"]["top_level"], ["README.md", "src", "tests"])
            self.assertEqual(profile["map"]["documentation"], ["README.md"])
            self.assertEqual(len(profile["source_fingerprint"]), hashlib.sha256().digest_size * 2)

    def test_profile_records_sorted_test_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
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
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial commit"],
                cwd=repository,
                check=True,
                capture_output=True,
            )
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}

            with_paths = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentflow",
                    "profile",
                    "--check",
                    "python3 -m unittest discover -s tests -v",
                    "--test-path",
                    "tests/unit",
                    "--test-path",
                    "tests/",
                ],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(with_paths.returncode, 0, with_paths.stderr)
            profile_path = repository / ".agentflow" / "repository-profile.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            # Trailing slash normalized and values recorded sorted.
            self.assertEqual(profile["test_paths"], ["tests", "tests/unit"])
            self.assertEqual(profile["schema_version"], 1)

            # Regenerating without the flag records no test_paths (no carry-forward).
            regenerated = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentflow",
                    "profile",
                    "--check",
                    "python3 -m unittest discover -s tests -v",
                ],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(regenerated.returncode, 0, regenerated.stderr)
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertNotIn("test_paths", profile)

    def test_profile_rejects_escaping_test_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
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
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial commit"],
                cwd=repository,
                check=True,
                capture_output=True,
            )
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}

            rejected = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentflow",
                    "profile",
                    "--check",
                    "python3 -m unittest discover -s tests -v",
                    "--test-path",
                    "../escape",
                ],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("must be repository-relative", rejected.stderr)

    def test_start_records_profile_reference_integrity_and_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "target"
            data_dir = temp_path / "agentflow-home"
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
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            profiled = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentflow",
                    "profile",
                    "--check",
                    "python3 -m unittest discover -s tests -v",
                ],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(profiled.returncode, 0, profiled.stderr)
            subprocess.run(
                ["git", "add", "-f", ".agentflow/repository-profile.json"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "Add repository profile"],
                cwd=repository,
                check=True,
                capture_output=True,
            )

            started = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentflow",
                    "start",
                    "Add health check",
                    "--data-dir",
                    str(data_dir),
                ],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(started.returncode, 0, started.stderr)
            run_id = json.loads(started.stdout)["run_id"]
            evidence = json.loads(
                (data_dir / "runs" / run_id / "profile.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(evidence["path"], ".agentflow/repository-profile.json")
            self.assertTrue(evidence["fresh"])
            self.assertEqual(len(evidence["profile_sha256"]), 64)
            self.assertEqual(len(evidence["source_fingerprint"]), 64)


if __name__ == "__main__":
    unittest.main()
