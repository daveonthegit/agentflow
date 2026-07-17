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
    save_work_graph,
    work_graph_content_hash,
)


HEALTH_ITEM = {
    "id": "health",
    "summary": "Add a health endpoint",
    "acceptance_criteria": ["GET /health returns 200"],
    "depends_on": [],
}

APPROVALS_RELATIVE = Path(".agentflow") / "approvals.jsonl"


def run_agentflow(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agentflow", *args],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )


def run_verify(repository: Path) -> subprocess.CompletedProcess[str]:
    # No --data-dir: the command must need no Agentflow Home state.
    return run_agentflow(
        "work", "verify", "--repository", str(repository), cwd=repository
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


def _approval_record(*, sequence: int, graph_hash: str, approved_by: str = "dave") -> dict:
    return {
        "approved_at": "2026-07-17T00:00:00+00:00",
        "approved_by": approved_by,
        "graph_hash": graph_hash,
        "repository": "/repo",
        "sequence": sequence,
        "type": "work_graph_approved",
    }


def _write_approvals(repository: Path, lines: list[str]) -> None:
    (repository / APPROVALS_RELATIVE).write_text(
        "".join(line + "\n" for line in lines), encoding="utf-8"
    )


def _current_hash() -> str:
    return work_graph_content_hash(validate_work_graph([HEALTH_ITEM]))


def _assert_no_traceback(result: subprocess.CompletedProcess[str]) -> None:
    assert "Traceback" not in result.stderr, result.stderr


class WorkVerifyExitZeroTests(unittest.TestCase):
    def test_exits_zero_when_graph_valid_and_approval_current(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "repo"
            data_dir = temp_path / "home"
            _init_repo(repository, [HEALTH_ITEM])
            approve_work_graph(
                repository=repository, data_dir=data_dir, approved_by="daveonthegit"
            )

            result = run_verify(repository)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                {
                    "approved_by": "daveonthegit",
                    "graph_hash": _current_hash(),
                    "sequence": 1,
                    "state": "verified",
                },
            )

    def test_runs_without_data_dir_or_home_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "repo"
            data_dir = temp_path / "home"
            _init_repo(repository, [HEALTH_ITEM])
            approve_work_graph(
                repository=repository, data_dir=data_dir, approved_by="dave"
            )

            # Simulate a CI runner / fresh machine: point Agentflow Home at an
            # empty dir. The result must be identical to running with real home
            # state present, because verify reads the repository alone.
            empty_home = temp_path / "empty-home"
            empty_home.mkdir()
            result = run_agentflow(
                "work",
                "verify",
                "--repository",
                str(repository),
                cwd=repository,
            )
            with_empty_home = subprocess.run(
                [sys.executable, "-m", "agentflow", "work", "verify",
                 "--repository", str(repository)],
                cwd=repository,
                env={
                    **os.environ,
                    "PYTHONPATH": str(PROJECT_ROOT / "src"),
                    "AGENTFLOW_HOME": str(empty_home),
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(with_empty_home.returncode, 0, with_empty_home.stderr)
            self.assertEqual(result.stdout, with_empty_home.stdout)
            self.assertFalse((empty_home).exists() and any(empty_home.iterdir()))


class WorkVerifyFailureModeTests(unittest.TestCase):
    def test_unapproved_graph_fails_with_actionable_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_repo(repository, [HEALTH_ITEM])

            result = run_verify(repository)
            self.assertNotEqual(result.returncode, 0)
            _assert_no_traceback(result)
            self.assertIn("no repo-tracked approval", result.stderr)
            self.assertIn("work approve", result.stderr)
            self.assertEqual(result.stdout, "")

    def test_stale_approval_after_graph_edit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "repo"
            data_dir = temp_path / "home"
            _init_repo(repository, [HEALTH_ITEM])
            approve_work_graph(
                repository=repository, data_dir=data_dir, approved_by="dave"
            )
            save_work_graph(
                [{**HEALTH_ITEM, "summary": "Changed summary"}], repository
            )

            result = run_verify(repository)
            self.assertNotEqual(result.returncode, 0)
            _assert_no_traceback(result)
            self.assertIn("changed after its approval", result.stderr)
            self.assertIn("re-approve", result.stderr)

    def test_invalid_graph_json_fails_with_line_number(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_repo(repository, [HEALTH_ITEM])
            (repository / ".agentflow" / "work" / "graph.jsonl").write_text(
                json.dumps(HEALTH_ITEM) + "\nnot json\n", encoding="utf-8"
            )

            result = run_verify(repository)
            self.assertNotEqual(result.returncode, 0)
            _assert_no_traceback(result)
            self.assertIn("graph.jsonl:2", result.stderr)
            self.assertIn("not valid JSON", result.stderr)

    def test_invalid_graph_schema_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_repo(repository, [HEALTH_ITEM])
            broken = {**HEALTH_ITEM, "depends_on": ["missing"]}
            (repository / ".agentflow" / "work" / "graph.jsonl").write_text(
                json.dumps(broken) + "\n", encoding="utf-8"
            )

            result = run_verify(repository)
            self.assertNotEqual(result.returncode, 0)
            _assert_no_traceback(result)
            self.assertIn("unknown ids", result.stderr)


class WorkVerifyApprovalLogTests(unittest.TestCase):
    def test_tolerates_torn_trailing_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_repo(repository, [HEALTH_ITEM])
            good = _approval_record(sequence=1, graph_hash=_current_hash())
            # A concurrent append can leave a partial trailing line.
            _write_approvals(
                repository,
                [json.dumps(good, sort_keys=True), '{"approved_at": "2026-'],
            )

            result = run_verify(repository)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["state"], "verified")

    def test_corrupt_non_trailing_line_fails_with_line_number(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_repo(repository, [HEALTH_ITEM])
            good = _approval_record(sequence=2, graph_hash=_current_hash())
            # A malformed line that is NOT the trailing line is genuine
            # corruption, not a torn append.
            _write_approvals(
                repository,
                ["not json at all", json.dumps(good, sort_keys=True)],
            )

            result = run_verify(repository)
            self.assertNotEqual(result.returncode, 0)
            _assert_no_traceback(result)
            self.assertIn("approvals.jsonl:1", result.stderr)
            self.assertIn("corrupt approval log", result.stderr)

    def test_schema_invalid_approval_record_fails_with_line_number(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_repo(repository, [HEALTH_ITEM])
            record = _approval_record(sequence=1, graph_hash=_current_hash())
            del record["approved_by"]
            _write_approvals(repository, [json.dumps(record, sort_keys=True)])

            result = run_verify(repository)
            self.assertNotEqual(result.returncode, 0)
            _assert_no_traceback(result)
            self.assertIn("approvals.jsonl:1", result.stderr)
            self.assertIn("approval record", result.stderr)

    def test_bad_graph_hash_in_record_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_repo(repository, [HEALTH_ITEM])
            record = _approval_record(sequence=1, graph_hash="not-a-hash")
            _write_approvals(repository, [json.dumps(record, sort_keys=True)])

            result = run_verify(repository)
            self.assertNotEqual(result.returncode, 0)
            _assert_no_traceback(result)
            self.assertIn("graph_hash", result.stderr)

    def test_latest_selected_by_highest_sequence_not_file_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_repo(repository, [HEALTH_ITEM])
            current = _approval_record(sequence=2, graph_hash=_current_hash())
            stale = _approval_record(sequence=1, graph_hash="0" * 64)
            # The binding (highest-sequence) record is written FIRST; a stale,
            # lower-sequence record is the last line in the file. Selecting by
            # file order would report stale; selecting by sequence passes.
            _write_approvals(
                repository,
                [
                    json.dumps(current, sort_keys=True),
                    json.dumps(stale, sort_keys=True),
                ],
            )

            result = run_verify(repository)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["sequence"], 2)

    def test_sequence_tie_with_differing_hashes_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_repo(repository, [HEALTH_ITEM])
            one = _approval_record(sequence=2, graph_hash=_current_hash())
            two = _approval_record(sequence=2, graph_hash="0" * 64)
            _write_approvals(
                repository,
                [json.dumps(one, sort_keys=True), json.dumps(two, sort_keys=True)],
            )

            result = run_verify(repository)
            self.assertNotEqual(result.returncode, 0)
            _assert_no_traceback(result)
            self.assertIn("ambiguous latest approval", result.stderr)
            self.assertIn("sequence 2", result.stderr)

    def test_wrong_typed_graph_hash_fails_without_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_repo(repository, [HEALTH_ITEM])
            # graph_hash is schema-declared as a 64-hex-char string; a
            # wrong-typed value (here, JSON null) must be reported as a
            # distinct, actionable ContractError like any other malformed
            # field, never surface as an uncaught TypeError/traceback.
            record = _approval_record(sequence=1, graph_hash=_current_hash())
            record["graph_hash"] = None
            _write_approvals(repository, [json.dumps(record, sort_keys=True)])

            result = run_verify(repository)
            self.assertNotEqual(result.returncode, 0)
            _assert_no_traceback(result)
            self.assertIn("approvals.jsonl:1", result.stderr)
            self.assertIn("graph_hash", result.stderr)

    def test_identical_duplicate_records_are_not_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_repo(repository, [HEALTH_ITEM])
            record = _approval_record(sequence=1, graph_hash=_current_hash())
            # A duplicated identical record shares a sequence but binds the same
            # hash: not ambiguous, so verify still passes.
            _write_approvals(
                repository,
                [json.dumps(record, sort_keys=True)] * 2,
            )

            result = run_verify(repository)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
