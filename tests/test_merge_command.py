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

import agentflow.merger as merger  # noqa: E402
from agentflow.run_kernel import acquire_claim  # noqa: E402

try:
    from tests.test_advance_command import agentflow, create_tested_run
except ImportError:  # unittest discover imports test modules without a package
    from test_advance_command import agentflow, create_tested_run


def read_events(data_dir: Path, run_id: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (data_dir / "runs" / run_id / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def create_approved_run(
    temp_path: Path,
    environment: dict[str, str],
    allow_merge: bool = True,
) -> tuple[Path, str, Path, str]:
    """Drive a fake-adapter Run to human_approved.

    Returns the data dir, run id, Target Repository path, and approved SHA.
    """
    data_dir, run_id = create_tested_run(
        temp_path, environment, allow_merge=allow_merge
    )
    fixture_path = temp_path / "reviewer-fixture.json"
    fixture_path.write_text(
        json.dumps({"reviewer": {"disposition": "approve", "findings": []}}),
        encoding="utf-8",
    )
    reviewed = agentflow(
        "advance",
        run_id,
        "--adapter",
        "fake",
        "--adapter-fixture",
        str(fixture_path),
        "--data-dir",
        str(data_dir),
        cwd=temp_path,
        environment=environment,
    )
    if reviewed.returncode != 0:
        raise AssertionError(reviewed.stderr)
    approved = agentflow(
        "approve",
        run_id,
        "--approved-by",
        "merge-test-human",
        "--data-dir",
        str(data_dir),
        cwd=temp_path,
        environment=environment,
    )
    if approved.returncode != 0:
        raise AssertionError(approved.stderr)
    approved_sha = json.loads(approved.stdout)["approved_sha"]
    status = agentflow(
        "status",
        run_id,
        "--data-dir",
        str(data_dir),
        cwd=temp_path,
        environment=environment,
    )
    repository = Path(json.loads(status.stdout)["repository"])
    return data_dir, run_id, repository, approved_sha


def repository_head(repository: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


class MergeCommandTests(unittest.TestCase):
    def test_merge_with_current_approval_merges_and_records_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id, repository, approved_sha = create_approved_run(
                temp_path, environment
            )

            merged = agentflow(
                "merge",
                run_id,
                "--merged-by",
                "merge-test-human",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertEqual(merged.returncode, 0, merged.stderr)
            response = json.loads(merged.stdout)
            self.assertEqual(response["state"], "merged")
            self.assertEqual(response["approved_sha"], approved_sha)
            # Fast-forward: the target branch now sits exactly at the
            # Approved Revision.
            self.assertEqual(response["merged_sha"], approved_sha)
            self.assertEqual(repository_head(repository), approved_sha)
            self.assertEqual(response["strategy"], "fast-forward")

            evidence = json.loads(
                (data_dir / "runs" / run_id / "merge.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(evidence["candidate_sha"], approved_sha)
            self.assertEqual(evidence["merged_sha"], approved_sha)
            self.assertEqual(
                evidence["approval"]["approved_by"], "merge-test-human"
            )
            self.assertEqual(evidence["approval"]["approved_sha"], approved_sha)
            self.assertIsInstance(evidence["approval"]["sequence"], int)
            self.assertEqual(evidence["policy"]["allow"], True)
            self.assertEqual(evidence["policy"]["strategy"], "fast-forward")
            self.assertEqual(
                evidence["policy"]["target_branch"], response["target_branch"]
            )

            events = read_events(data_dir, run_id)
            completed = next(
                event for event in events if event["type"] == "merge_completed"
            )
            self.assertEqual(completed["candidate_sha"], approved_sha)
            self.assertEqual(completed["merged_sha"], approved_sha)
            # The merger recorded merge evidence but granted no approval: the
            # single human_approved event remains the one the human appended.
            self.assertEqual(
                sum(1 for event in events if event["type"] == "human_approved"),
                1,
            )
            status = agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(json.loads(status.stdout)["state"], "merged")

    def test_stale_approval_refuses_the_merge_with_recorded_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id, repository, approved_sha = create_approved_run(
                temp_path, environment
            )
            head_before = repository_head(repository)
            worktree = Path(
                json.loads(
                    agentflow(
                        "status",
                        run_id,
                        "--data-dir",
                        str(data_dir),
                        cwd=temp_path,
                        environment=environment,
                    ).stdout
                )["worktree"]
            )
            # The candidate drifts after approval: any new commit means the
            # merge candidate no longer matches the Approved Revision.
            (worktree / "DRIFT.md").write_text("drift\n", encoding="utf-8")
            subprocess.run(["git", "add", "DRIFT.md"], cwd=worktree, check=True)
            subprocess.run(
                ["git", "commit", "-m", "drift"],
                cwd=worktree,
                check=True,
                capture_output=True,
            )

            merged = agentflow(
                "merge",
                run_id,
                "--merged-by",
                "merge-test-human",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertNotEqual(merged.returncode, 0)
            self.assertIn("approval is stale", merged.stderr)
            self.assertEqual(repository_head(repository), head_before)
            self.assertFalse((data_dir / "runs" / run_id / "merge.json").exists())
            events = read_events(data_dir, run_id)
            refusal = next(
                event for event in events if event["type"] == "merge_refused"
            )
            self.assertIn("approval is stale", refusal["reason"])
            self.assertEqual(refusal["approved_sha"], approved_sha)
            self.assertFalse(
                any(event["type"] == "merge_completed" for event in events)
            )

    def test_missing_merge_policy_refuses_the_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id, repository, _ = create_approved_run(
                temp_path, environment, allow_merge=False
            )
            head_before = repository_head(repository)

            merged = agentflow(
                "merge",
                run_id,
                "--merged-by",
                "merge-test-human",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertNotEqual(merged.returncode, 0)
            self.assertIn("merging is not permitted", merged.stderr)
            self.assertEqual(repository_head(repository), head_before)
            events = read_events(data_dir, run_id)
            refusal = next(
                event for event in events if event["type"] == "merge_refused"
            )
            self.assertIn("no merge_policy", refusal["reason"])
            self.assertFalse(
                any(event["type"] == "merge_completed" for event in events)
            )

    def test_target_branch_mismatch_refuses_the_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id, repository, _ = create_approved_run(
                temp_path, environment
            )
            # The repository is no longer on the branch the committed policy
            # targets: repository policy must refuse the merge.
            subprocess.run(
                ["git", "checkout", "-b", "not-the-target"],
                cwd=repository,
                check=True,
                capture_output=True,
            )
            head_before = repository_head(repository)

            merged = agentflow(
                "merge",
                run_id,
                "--merged-by",
                "merge-test-human",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertNotEqual(merged.returncode, 0)
            self.assertIn("not-the-target", merged.stderr)
            self.assertEqual(repository_head(repository), head_before)
            events = read_events(data_dir, run_id)
            refusal = next(
                event for event in events if event["type"] == "merge_refused"
            )
            self.assertIn("merge_policy targets", refusal["reason"])

    def test_merge_refused_without_human_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_tested_run(
                temp_path, environment, allow_merge=True
            )

            merged = agentflow(
                "merge",
                run_id,
                "--merged-by",
                "merge-test-human",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertNotEqual(merged.returncode, 0)
            self.assertIn("cannot merge from state tested", merged.stderr)
            events = read_events(data_dir, run_id)
            self.assertTrue(
                any(event["type"] == "merge_refused" for event in events)
            )
            # The merger never manufactures the approval it is missing.
            self.assertFalse(
                any(event["type"] == "human_approved" for event in events)
            )

    def test_second_merge_is_refused_and_evidence_stays_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id, _, _ = create_approved_run(temp_path, environment)
            first = agentflow(
                "merge",
                run_id,
                "--merged-by",
                "merge-test-human",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            evidence_before = (data_dir / "runs" / run_id / "merge.json").read_text(
                encoding="utf-8"
            )

            second = agentflow(
                "merge",
                run_id,
                "--merged-by",
                "merge-test-human",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertNotEqual(second.returncode, 0)
            self.assertIn("cannot merge from state merged", second.stderr)
            self.assertEqual(
                (data_dir / "runs" / run_id / "merge.json").read_text(
                    encoding="utf-8"
                ),
                evidence_before,
            )

    def test_merge_refused_while_stage_is_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id, repository, _ = create_approved_run(
                temp_path, environment
            )
            head_before = repository_head(repository)
            acquire_claim(
                data_dir=data_dir, run_id=run_id, holder="other-process"
            )

            with self.assertRaises(Exception):
                merger.merge_approved_run(
                    run_id=run_id,
                    merged_by="merge-test-human",
                    data_dir=data_dir,
                )

            self.assertEqual(repository_head(repository), head_before)
            events = read_events(data_dir, run_id)
            self.assertFalse(
                any(event["type"] == "merge_completed" for event in events)
            )

    def test_merger_cannot_append_approval_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id, _, approved_sha = create_approved_run(
                temp_path, environment
            )
            events_before = read_events(data_dir, run_id)

            # The merger's only event writer structurally refuses approval
            # (and every other non-merge) event type before touching the log.
            for event_type in ("human_approved", "build_ready", "run_created"):
                with self.assertRaises(ValueError):
                    merger.append_merge_event(
                        data_dir=data_dir,
                        run_id=run_id,
                        event_type=event_type,
                        holder="merger-test",
                        approved_sha=approved_sha,
                    )

            self.assertEqual(read_events(data_dir, run_id), events_before)
            # The approval-granting command is not reachable from the merger
            # module at all.
            self.assertFalse(hasattr(merger, "approve_run"))


if __name__ == "__main__":
    unittest.main()
