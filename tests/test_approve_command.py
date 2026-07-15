from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).parents[1]


def run_agentflow(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agentflow", *args],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )


class ApproveCommandTests(unittest.TestCase):
    def test_approve_records_explicit_human_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir = temp_path / "agentflow-home"
            workspace = temp_path / "workspace"
            run_id = "run-awaiting-human"
            run_dir = data_dir / "runs" / run_id
            run_dir.mkdir(parents=True)
            workspace.mkdir()
            subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "agentflow@example.test"],
                cwd=workspace,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Agentflow Test"],
                cwd=workspace,
                check=True,
            )
            (workspace / "README.md").write_text("# Candidate\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Candidate"],
                cwd=workspace,
                check=True,
                capture_output=True,
            )
            candidate_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            events = (
                {"run_id": run_id, "sequence": 1, "type": "run_created"},
                {
                    "sequence": 2,
                    "type": "workspace_ready",
                    "worktree": str(workspace),
                },
                {
                    "candidate_sha": candidate_sha,
                    "sequence": 3,
                    "type": "awaiting_human",
                },
            )
            (run_dir / "events.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )

            approved = run_agentflow(
                "approve",
                run_id,
                "--approved-by",
                "daveonthegit",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
            )

            self.assertEqual(approved.returncode, 0, approved.stderr)
            self.assertEqual(
                json.loads(approved.stdout),
                {
                    "approved_by": "daveonthegit",
                    "approved_sha": candidate_sha,
                    "run_id": run_id,
                    "state": "human_approved",
                },
            )
            status = run_agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            status_response = json.loads(status.stdout)
            self.assertEqual(status_response["state"], "human_approved")
            self.assertEqual(status_response["approved_sha"], candidate_sha)


if __name__ == "__main__":
    unittest.main()
