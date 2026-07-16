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

from agentflow.run_kernel import acquire_claim  # noqa: E402

try:
    from tests.test_advance_command import (
        advance_tester,
        agentflow,
        create_built_run,
        create_profiled_run,
        create_tested_run,
        create_verified_run,
    )
except ImportError:  # unittest discover imports test modules without a package
    from test_advance_command import (
        advance_tester,
        agentflow,
        create_built_run,
        create_profiled_run,
        create_tested_run,
        create_verified_run,
    )


def read_events(data_dir: Path, run_id: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (data_dir / "runs" / run_id / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def non_claim_events(events: list[dict]) -> list[dict]:
    claim_types = {"claim_acquired", "claim_released", "claim_expired"}
    return [event for event in events if event["type"] not in claim_types]


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def commit_on_main(repository: Path, path: str, content: str, message: str) -> str:
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", path], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return git("rev-parse", "HEAD", cwd=repository)


def status_json(
    temp_path: Path,
    data_dir: Path,
    run_id: str,
    environment: dict[str, str],
) -> dict:
    status = agentflow(
        "status",
        run_id,
        "--data-dir",
        str(data_dir),
        cwd=temp_path,
        environment=environment,
    )
    if status.returncode != 0:
        raise AssertionError(status.stderr)
    return json.loads(status.stdout)


def rebase(
    temp_path: Path,
    data_dir: Path,
    run_id: str,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return agentflow(
        "rebase",
        run_id,
        "--data-dir",
        str(data_dir),
        cwd=temp_path,
        environment=environment,
    )


def write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


REVIEWER_APPROVE_STUB = """#!/usr/bin/env python3
import json
import sys

assert "--output-format" in sys.argv
assert "reviewer" in sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({
    "type": "assistant",
    "message": {"content": "pre-rebase review of candidate"},
}))
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "reviewed",
    "structured_output": {"disposition": "approve", "findings": []},
}))
"""

REVIEWER_POST_REBASE_STUB = """#!/usr/bin/env python3
import json
import sys

assert "--output-format" in sys.argv
assert "reviewer" in sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({
    "type": "assistant",
    "message": {"content": "post-rebase review of rebased candidate"},
}))
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "reviewed",
    "structured_output": {
        "disposition": "approve",
        "findings": [{
            "file": None,
            "message": "Post-rebase review of rebased candidate",
            "severity": "note",
        }],
    },
}))
"""


class RebaseCommandTests(unittest.TestCase):
    def test_rebase_on_up_to_date_run_appends_no_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_built_run(temp_path, environment)
            events_path = data_dir / "runs" / run_id / "events.jsonl"
            before = events_path.read_text(encoding="utf-8")

            rebased = rebase(temp_path, data_dir, run_id, environment)

            self.assertEqual(rebased.returncode, 0, rebased.stderr)
            response = json.loads(rebased.stdout)
            self.assertFalse(response["rebased"])
            self.assertEqual(response["state"], "built")
            self.assertEqual(
                events_path.read_text(encoding="utf-8"), before
            )

    def test_rebase_refreshes_candidate_onto_advanced_main(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_built_run(temp_path, environment)
            repository = temp_path / "target"
            status_before = status_json(temp_path, data_dir, run_id, environment)
            old_base = status_before["base_sha"]
            old_candidate = status_before["candidate_sha"]
            new_base = commit_on_main(
                repository, "NOTES.md", "extra notes\n", "Add notes on main"
            )
            target_head_before = git("rev-parse", "HEAD", cwd=repository)
            target_branch_before = git(
                "rev-parse", "--abbrev-ref", "HEAD", cwd=repository
            )

            rebased = rebase(temp_path, data_dir, run_id, environment)

            self.assertEqual(rebased.returncode, 0, rebased.stderr)
            response = json.loads(rebased.stdout)
            self.assertTrue(response["rebased"])
            self.assertEqual(response["state"], "built")
            self.assertEqual(response["old_base_sha"], old_base)
            self.assertEqual(response["new_base_sha"], new_base)
            self.assertEqual(response["old_candidate_sha"], old_candidate)
            new_candidate = response["new_candidate_sha"]
            self.assertNotEqual(new_candidate, old_candidate)

            # The Target Repository's primary checkout is untouched.
            self.assertEqual(git("rev-parse", "HEAD", cwd=repository), target_head_before)
            self.assertEqual(
                git("rev-parse", "--abbrev-ref", "HEAD", cwd=repository),
                target_branch_before,
            )

            status_after = status_json(temp_path, data_dir, run_id, environment)
            self.assertEqual(status_after["state"], "built")
            self.assertEqual(status_after["base_sha"], new_base)
            self.assertEqual(status_after["candidate_sha"], new_candidate)

            listing = agentflow(
                "list",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(listing.returncode, 0, listing.stderr)
            entry = next(
                item
                for item in json.loads(listing.stdout)
                if item["run_id"] == run_id
            )
            self.assertEqual(entry["base_sha"], new_base)
            self.assertEqual(entry["candidate_sha"], new_candidate)

            # Re-verify the rebased candidate and approve it.
            verified = agentflow(
                "advance",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertEqual(json.loads(verified.stdout)["state"], "verified")
            self.assertEqual(
                json.loads(verified.stdout)["candidate_sha"], new_candidate
            )
            advance_tester(temp_path, data_dir, run_id, environment)
            fixture_path = temp_path / "adapter-fixture.json"
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
            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
            self.assertEqual(json.loads(reviewed.stdout)["state"], "awaiting_human")
            approved = agentflow(
                "approve",
                run_id,
                "--approved-by",
                "integration-test-human",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)
            self.assertEqual(json.loads(approved.stdout)["approved_sha"], new_candidate)

    def test_rebase_conflict_leaves_run_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_built_run(temp_path, environment)
            repository = temp_path / "target"
            status_before = status_json(temp_path, data_dir, run_id, environment)
            base_before = status_before["base_sha"]
            candidate_before = status_before["candidate_sha"]
            workspace = Path(status_before["worktree"])
            workspace_head_before = git("rev-parse", "HEAD", cwd=workspace)
            events_before = non_claim_events(read_events(data_dir, run_id))
            # Conflict: rewrite the same README.md the builder rewrote.
            commit_on_main(
                repository,
                "README.md",
                "# Completely different heading\n",
                "Rewrite README on main",
            )

            rebased = rebase(temp_path, data_dir, run_id, environment)

            self.assertNotEqual(rebased.returncode, 0)
            self.assertIn("conflict", rebased.stderr)
            status_after = status_json(temp_path, data_dir, run_id, environment)
            self.assertEqual(status_after["state"], "built")
            self.assertEqual(status_after["base_sha"], base_before)
            self.assertEqual(status_after["candidate_sha"], candidate_before)
            self.assertEqual(
                git("rev-parse", "HEAD", cwd=workspace), workspace_head_before
            )
            self.assertFalse(
                git("status", "--porcelain", "--untracked-files=all", cwd=workspace)
            )
            events_after = read_events(data_dir, run_id)
            self.assertEqual(non_claim_events(events_after), events_before)
            self.assertFalse(
                any(e["type"] == "candidate_rebased" for e in events_after)
            )
            # No rebase is left in progress in the Workspace.
            abort_again = subprocess.run(
                ["git", "rebase", "--abort"],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(abort_again.returncode, 0)

    def test_rebase_fails_on_pre_candidate_ready_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            repository, data_dir, run_id = create_profiled_run(temp_path, environment)
            # Advance main so the up-to-date fast path does not short-circuit.
            commit_on_main(repository, "NOTES.md", "notes\n", "Advance main")
            events_before = read_events(data_dir, run_id)

            rebased = rebase(temp_path, data_dir, run_id, environment)

            self.assertNotEqual(rebased.returncode, 0)
            self.assertIn(
                f"run {run_id} cannot be rebased from state ready", rebased.stderr
            )
            self.assertEqual(
                non_claim_events(read_events(data_dir, run_id)), events_before
            )

    def test_rebase_fails_on_an_abandoned_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            repository, data_dir, run_id = create_profiled_run(temp_path, environment)
            abandoned = agentflow(
                "abandon",
                run_id,
                "--abandoned-by",
                "rebase-test",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(abandoned.returncode, 0, abandoned.stderr)
            commit_on_main(repository, "NOTES.md", "notes\n", "Advance main")

            rebased = rebase(temp_path, data_dir, run_id, environment)

            self.assertNotEqual(rebased.returncode, 0)
            self.assertIn(
                f"run {run_id} cannot be rebased from state abandoned",
                rebased.stderr,
            )

    def test_rebase_fails_on_a_human_approved_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_tested_run(temp_path, environment)
            fixture_path = temp_path / "adapter-fixture.json"
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
            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
            approved = agentflow(
                "approve",
                run_id,
                "--approved-by",
                "integration-test-human",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)
            commit_on_main(temp_path / "target", "MORE.md", "more\n", "Advance main")

            rebased = rebase(temp_path, data_dir, run_id, environment)

            self.assertNotEqual(rebased.returncode, 0)
            self.assertIn(
                f"run {run_id} cannot be rebased from state human_approved",
                rebased.stderr,
            )

    def test_rebase_is_rejected_while_a_foreign_claim_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id = create_built_run(temp_path, environment)
            repository = temp_path / "target"
            # Advance main so the fast path does not short-circuit before the claim.
            commit_on_main(repository, "NOTES.md", "notes\n", "Advance main")
            acquire_claim(
                data_dir=data_dir,
                run_id=run_id,
                holder="other-process",
                lease_seconds=100000,
            )
            events_path = data_dir / "runs" / run_id / "events.jsonl"
            events_before = events_path.read_text(encoding="utf-8")

            rebased = rebase(temp_path, data_dir, run_id, environment)

            self.assertNotEqual(rebased.returncode, 0)
            self.assertIn("other-process", rebased.stderr)
            self.assertEqual(events_path.read_text(encoding="utf-8"), events_before)

    def test_rebase_then_checks_and_review_preserve_prior_attempt_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("AGENTFLOW_CLAUDE")
            }
            environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
            data_dir, run_id = create_verified_run(temp_path, environment)
            advance_tester(temp_path, data_dir, run_id, environment)
            fake_claude = temp_path / "claude"
            write_executable(fake_claude, REVIEWER_APPROVE_STUB)
            claude_environment = {
                **environment,
                "AGENTFLOW_CLAUDE": str(fake_claude),
            }
            reviewed = agentflow(
                "advance",
                run_id,
                "--adapter",
                "claude",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=claude_environment,
            )
            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
            self.assertEqual(json.loads(reviewed.stdout)["state"], "awaiting_human")

            run_dir = data_dir / "runs" / run_id
            pre_artifacts = {
                path.name: (path, path.read_bytes())
                for path in (
                    run_dir / "build-report-1.json",
                    run_dir / "checks-1.json",
                    run_dir / "review-1.json",
                    run_dir / "reviewer-1-transcript.jsonl",
                )
            }
            for path, _ in pre_artifacts.values():
                self.assertTrue(path.is_file(), path.name)
            pre_events = read_events(data_dir, run_id)
            pre_checks_event = next(
                e for e in reversed(pre_events) if e["type"] == "checks_passed"
            )
            pre_review_event = next(
                e for e in reversed(pre_events) if e["type"] == "review_ready"
            )
            self.assertEqual(
                Path(pre_checks_event["artifact"]).resolve(),
                (run_dir / "checks-1.json").resolve(),
            )
            self.assertEqual(
                Path(pre_review_event["artifact"]).resolve(),
                (run_dir / "review-1.json").resolve(),
            )
            self.assertEqual(
                Path(pre_review_event["transcript"]).resolve(),
                (run_dir / "reviewer-1-transcript.jsonl").resolve(),
            )

            repository = temp_path / "target"
            commit_on_main(repository, "NOTES.md", "notes\n", "Advance main")
            rebased = rebase(temp_path, data_dir, run_id, environment)
            self.assertEqual(rebased.returncode, 0, rebased.stderr)
            rebased_response = json.loads(rebased.stdout)
            self.assertTrue(rebased_response["rebased"])
            self.assertEqual(rebased_response["state"], "built")
            new_candidate = rebased_response["new_candidate_sha"]

            status_after_rebase = status_json(temp_path, data_dir, run_id, environment)
            self.assertEqual(status_after_rebase["state"], "built")
            self.assertEqual(status_after_rebase["candidate_sha"], new_candidate)

            rechecked = agentflow(
                "advance",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(rechecked.returncode, 0, rechecked.stderr)
            rechecked_response = json.loads(rechecked.stdout)
            self.assertEqual(rechecked_response["state"], "verified")
            self.assertEqual(rechecked_response["candidate_sha"], new_candidate)
            checks_2 = run_dir / "checks-2.json"
            self.assertTrue(checks_2.is_file())
            self.assertNotEqual(
                checks_2.read_bytes(),
                pre_artifacts["checks-1.json"][1],
            )

            advance_tester(temp_path, data_dir, run_id, environment)
            write_executable(fake_claude, REVIEWER_POST_REBASE_STUB)
            rereviewed = agentflow(
                "advance",
                run_id,
                "--adapter",
                "claude",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=claude_environment,
            )
            self.assertEqual(rereviewed.returncode, 0, rereviewed.stderr)
            self.assertEqual(json.loads(rereviewed.stdout)["state"], "awaiting_human")
            review_2 = run_dir / "review-2.json"
            reviewer_2_transcript = run_dir / "reviewer-2-transcript.jsonl"
            self.assertTrue(review_2.is_file())
            self.assertTrue(reviewer_2_transcript.is_file())
            self.assertNotEqual(
                review_2.read_bytes(),
                pre_artifacts["review-1.json"][1],
            )
            self.assertNotEqual(
                reviewer_2_transcript.read_bytes(),
                pre_artifacts["reviewer-1-transcript.jsonl"][1],
            )
            self.assertNotEqual(
                reviewer_2_transcript.resolve(),
                pre_artifacts["reviewer-1-transcript.jsonl"][0].resolve(),
            )

            # Attempt-1 evidence must remain present and byte-identical.
            for path, expected_bytes in pre_artifacts.values():
                self.assertTrue(path.is_file(), path.name)
                self.assertEqual(path.read_bytes(), expected_bytes, path.name)

            # Post-rebase events must bind to generation-2 artifacts/transcripts.
            post_events = read_events(data_dir, run_id)
            post_checks_events = [
                e for e in post_events if e["type"] == "checks_passed"
            ]
            post_review_events = [
                e for e in post_events if e["type"] == "review_ready"
            ]
            self.assertEqual(len(post_checks_events), 2)
            self.assertEqual(len(post_review_events), 2)
            self.assertEqual(
                Path(post_checks_events[-1]["artifact"]).resolve(),
                checks_2.resolve(),
            )
            self.assertEqual(
                post_checks_events[-1]["candidate_sha"], new_candidate
            )
            self.assertEqual(
                Path(post_review_events[-1]["artifact"]).resolve(),
                review_2.resolve(),
            )
            self.assertEqual(
                Path(post_review_events[-1]["transcript"]).resolve(),
                reviewer_2_transcript.resolve(),
            )
            self.assertEqual(
                post_review_events[-1]["candidate_sha"], new_candidate
            )

            status_final = status_json(temp_path, data_dir, run_id, environment)
            self.assertEqual(status_final["state"], "awaiting_human")
            self.assertEqual(status_final["candidate_sha"], new_candidate)


if __name__ == "__main__":
    unittest.main()
