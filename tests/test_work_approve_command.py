from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentflow.contracts import validate_work_graph  # noqa: E402
from agentflow.work_graph import (  # noqa: E402
    approve_work_graph,
    read_work_graph_approvals,
    require_approved_work_graph,
    save_work_graph,
    work_graph_content_hash,
)


HEALTH_ITEM = {
    "id": "health",
    "summary": "Add a health endpoint",
    "acceptance_criteria": ["GET /health returns 200"],
    "depends_on": [],
}


def run_agentflow(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agentflow", *args],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )


def _init_repo(repository: Path, items: list[dict]) -> None:
    repository.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "agentflow@example.test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Agentflow Test"], cwd=repository, check=True
    )
    (repository / "README.md").write_text("# Target\n", encoding="utf-8")
    work_dir = repository / ".agentflow" / "work"
    work_dir.mkdir(parents=True)
    (work_dir / "graph.jsonl").write_text(
        "\n".join(json.dumps(item) for item in items) + "\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Init with work graph"],
        cwd=repository,
        check=True,
        capture_output=True,
    )


def _commit_graph(repository: Path, items: list[dict]) -> None:
    save_work_graph(items, repository)
    subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Edit work graph"],
        cwd=repository,
        check=True,
        capture_output=True,
    )


class WorkGraphApprovalRecordTests(unittest.TestCase):
    def test_work_approve_writes_attributed_content_hashed_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "repo"
            data_dir = temp_path / "home"
            _init_repo(repository, [HEALTH_ITEM])

            approved = run_agentflow(
                "work",
                "approve",
                "--approved-by",
                "daveonthegit",
                "--repository",
                str(repository),
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
            )

            self.assertEqual(approved.returncode, 0, approved.stderr)
            expected_hash = work_graph_content_hash(
                validate_work_graph([HEALTH_ITEM])
            )
            self.assertEqual(
                json.loads(approved.stdout),
                {
                    "approved_by": "daveonthegit",
                    "graph_hash": expected_hash,
                    "repository": str(repository.resolve()),
                    "state": "work_graph_approved",
                },
            )
            records = read_work_graph_approvals(data_dir)
            self.assertEqual(len(records), 1)
            record = records[0]
            # A distinct evidence record from candidate approval: it approves
            # work intent by content hash, not a revision by SHA.
            self.assertEqual(record["type"], "work_graph_approved")
            self.assertNotEqual(record["type"], "human_approved")
            self.assertNotIn("approved_sha", record)
            self.assertEqual(record["approved_by"], "daveonthegit")
            self.assertEqual(record["graph_hash"], expected_hash)
            self.assertEqual(record["repository"], str(repository.resolve()))
            self.assertEqual(record["sequence"], 1)
            self.assertIn("approved_at", record)

    def test_repeat_approvals_append_with_contiguous_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "repo"
            data_dir = temp_path / "home"
            _init_repo(repository, [HEALTH_ITEM])

            approve_work_graph(
                repository=repository, data_dir=data_dir, approved_by="first"
            )
            approve_work_graph(
                repository=repository, data_dir=data_dir, approved_by="second"
            )

            records = read_work_graph_approvals(data_dir)
            self.assertEqual(
                [record["sequence"] for record in records], [1, 2]
            )
            self.assertEqual(
                [record["approved_by"] for record in records],
                ["first", "second"],
            )

    def test_work_approve_requires_approved_by_and_a_nonempty_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "repo"
            data_dir = temp_path / "home"
            _init_repo(repository, [HEALTH_ITEM])

            missing = run_agentflow(
                "work",
                "approve",
                "--repository",
                str(repository),
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("--approved-by", missing.stderr)

            empty_repository = temp_path / "empty"
            (empty_repository / ".agentflow" / "work").mkdir(parents=True)
            with self.assertRaises(ValueError):
                approve_work_graph(
                    repository=empty_repository,
                    data_dir=data_dir,
                    approved_by="daveonthegit",
                )


class CaptureGateTests(unittest.TestCase):
    def test_capture_allowed_only_while_graph_hash_matches_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "repo"
            data_dir = temp_path / "home"
            _init_repo(repository, [HEALTH_ITEM])

            # Unapproved: capture is refused and points at `work approve`.
            refused = run_agentflow(
                "start",
                "--work-item",
                "health",
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("not approved", refused.stderr)
            self.assertIn("work approve", refused.stderr)

            approved = run_agentflow(
                "work",
                "approve",
                "--approved-by",
                "daveonthegit",
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)

            # Approved and unchanged: capture succeeds.
            started = run_agentflow(
                "start",
                "--work-item",
                "health",
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            self.assertEqual(
                json.loads(started.stdout)["work_item_id"], "health"
            )

    def test_capture_refused_after_graph_mutation_until_reapproved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "repo"
            data_dir = temp_path / "home"
            _init_repo(repository, [HEALTH_ITEM])
            approve_work_graph(
                repository=repository,
                data_dir=data_dir,
                approved_by="daveonthegit",
            )

            # The graph changes after approval; the approval no longer binds.
            _commit_graph(
                repository, [{**HEALTH_ITEM, "summary": "Changed summary"}]
            )
            refused = run_agentflow(
                "start",
                "--work-item",
                "health",
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("changed after its approval", refused.stderr)
            self.assertIn("re-approve", refused.stderr)
            # Refusal leaves no Run behind.
            self.assertFalse((data_dir / "runs").exists())

            # Re-approving the current content restores capture.
            approve_work_graph(
                repository=repository,
                data_dir=data_dir,
                approved_by="daveonthegit",
            )
            started = run_agentflow(
                "start",
                "--work-item",
                "health",
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )
            self.assertEqual(started.returncode, 0, started.stderr)

    def test_require_approved_work_graph_binds_to_latest_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "repo"
            data_dir = temp_path / "home"
            _init_repo(repository, [HEALTH_ITEM])

            with self.assertRaises(ValueError):
                require_approved_work_graph(
                    repository=repository, data_dir=data_dir
                )

            approve_work_graph(
                repository=repository, data_dir=data_dir, approved_by="a"
            )
            self.assertEqual(
                require_approved_work_graph(
                    repository=repository, data_dir=data_dir
                ),
                work_graph_content_hash(validate_work_graph([HEALTH_ITEM])),
            )

            # Reverting to previously approved content is not enough: like an
            # Approved Revision, only the latest approval binds.
            _commit_graph(
                repository, [{**HEALTH_ITEM, "summary": "Changed summary"}]
            )
            approve_work_graph(
                repository=repository, data_dir=data_dir, approved_by="a"
            )
            _commit_graph(repository, [HEALTH_ITEM])
            with self.assertRaises(ValueError):
                require_approved_work_graph(
                    repository=repository, data_dir=data_dir
                )

    def test_reconcile_refuses_dispatch_from_an_unapproved_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "repo"
            data_dir = temp_path / "home"
            _init_repo(repository, [HEALTH_ITEM])

            report = run_agentflow(
                "reconcile",
                "--repository",
                str(repository),
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )
            self.assertNotEqual(report.returncode, 0)
            self.assertIn("not approved", report.stderr)
            self.assertFalse((data_dir / "runs").exists())


if __name__ == "__main__":
    unittest.main()
