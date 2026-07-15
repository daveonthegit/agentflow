from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).parents[1]


class InitCommandTests(unittest.TestCase):
    def test_init_configures_the_current_repository_for_ai_use(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            agents_path = repository / "AGENTS.md"
            agents_path.write_text("# Existing project instructions\n", encoding="utf-8")

            result = subprocess.run(
                [sys.executable, "-m", "agentflow", "init"],
                cwd=repository,
                env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual(response["state"], "initialized")
            self.assertEqual(Path(response["repository"]), repository.resolve())

            skill_path = repository / ".agents" / "skills" / "agentflow" / "SKILL.md"
            self.assertTrue(skill_path.is_file())
            distributable_skill = (
                PROJECT_ROOT / "skills" / "agentflow" / "SKILL.md"
            ).read_text(encoding="utf-8")
            self.assertEqual(
                skill_path.read_text(encoding="utf-8"),
                distributable_skill,
            )
            self.assertTrue(
                (
                    repository
                    / ".agents"
                    / "skills"
                    / "agentflow"
                    / "agents"
                    / "openai.yaml"
                ).is_file()
            )

            instructions = agents_path.read_text(encoding="utf-8")
            self.assertIn("# Existing project instructions", instructions)
            self.assertIn("## Agentflow", instructions)

    def test_init_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            command = [sys.executable, "-m", "agentflow", "init"]
            environment = {
                **os.environ,
                "PYTHONPATH": str(PROJECT_ROOT / "src"),
            }

            for _ in range(2):
                result = subprocess.run(
                    command,
                    cwd=repository,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            instructions = (repository / "AGENTS.md").read_text(encoding="utf-8")
            self.assertEqual(instructions.count("## Agentflow"), 1)


if __name__ == "__main__":
    unittest.main()
