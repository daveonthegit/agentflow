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

import agentflow.post_merge as post_merge  # noqa: E402

try:
    from tests.test_advance_command import (
        advance_tester,
        agentflow,
        create_verified_run,
    )
    from tests.test_merge_command import (
        create_approved_run,
        read_events,
        repository_head,
    )
except ImportError:  # unittest discover imports test modules without a package
    from test_advance_command import (
        advance_tester,
        agentflow,
        create_verified_run,
    )
    from test_merge_command import (
        create_approved_run,
        read_events,
        repository_head,
    )


HUMAN = "post-merge-test-human"

# Passes while the named file is absent from the checkout the check runs in.
# With a repository-relative path this proves which exact commit was checked
# out; with an absolute path it is an external switch the test can flip to
# make the authoritative checks fail only at post-merge verification time.
def _absent_file_check(path: str) -> str:
    return (
        'python3 -c "import pathlib, sys; '
        f"sys.exit(1 if pathlib.Path('{path}').exists() else 0)\""
    )


def _repository_clean(repository: Path) -> bool:
    return not subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def create_approved_run_with_check(
    temp_path: Path,
    environment: dict[str, str],
    check: str,
) -> tuple[Path, str, Path, str]:
    """Drive a fake-adapter Run with a custom profile check to human_approved."""
    data_dir, run_id = create_verified_run(
        temp_path, environment, check, allow_merge=True
    )
    advance_tester(temp_path, data_dir, run_id, environment)
    fixture_path = temp_path / "post-merge-reviewer-fixture.json"
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
        HUMAN,
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


def merge_run(
    temp_path: Path,
    data_dir: Path,
    run_id: str,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return agentflow(
        "merge",
        run_id,
        "--merged-by",
        HUMAN,
        "--data-dir",
        str(data_dir),
        cwd=temp_path,
        environment=environment,
    )


def verify_merge(
    temp_path: Path,
    data_dir: Path,
    run_id: str,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return agentflow(
        "verify-merge",
        run_id,
        "--verified-by",
        HUMAN,
        "--data-dir",
        str(data_dir),
        cwd=temp_path,
        environment=environment,
    )


def drive_second_run_to_approved(
    temp_path: Path,
    repository: Path,
    data_dir: Path,
    environment: dict[str, str],
    check: str,
) -> str:
    """Start and approve a second Run in the same Target Repository.

    The first merge changed the repository content, so the committed profile
    fingerprint is stale; regenerate and commit it before starting.
    """
    profiled = agentflow(
        "profile",
        "--check",
        check,
        "--test-path",
        "tests",
        "--allow-merge",
        cwd=repository,
        environment=environment,
    )
    if profiled.returncode != 0:
        raise AssertionError(profiled.stderr)
    subprocess.run(
        ["git", "add", "-f", ".agentflow/repository-profile.json"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Refresh repository profile"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    started = agentflow(
        "start",
        "Add release notes",
        "--data-dir",
        str(data_dir),
        cwd=repository,
        environment=environment,
    )
    if started.returncode != 0:
        raise AssertionError(started.stderr)
    run_id = json.loads(started.stdout)["run_id"]
    fixture_path = temp_path / "second-run-builder-fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "builder": {
                    "output": {
                        "commands_run": [],
                        "files_changed": ["NOTES.md"],
                        "steps_completed": ["P1"],
                        "unresolved_issues": [],
                    },
                    "writes": {"NOTES.md": "# Release notes\n"},
                }
            }
        ),
        encoding="utf-8",
    )
    built = agentflow(
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
    if built.returncode != 0:
        raise AssertionError(built.stderr)
    verified = agentflow(
        "advance",
        run_id,
        "--data-dir",
        str(data_dir),
        cwd=temp_path,
        environment=environment,
    )
    if verified.returncode != 0:
        raise AssertionError(verified.stderr)
    if json.loads(verified.stdout)["state"] != "verified":
        raise AssertionError(verified.stdout)
    advance_tester(temp_path, data_dir, run_id, environment)
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
        HUMAN,
        "--data-dir",
        str(data_dir),
        cwd=temp_path,
        environment=environment,
    )
    if approved.returncode != 0:
        raise AssertionError(approved.stderr)
    return run_id


class PostMergeVerificationTests(unittest.TestCase):
    def test_verification_runs_against_the_exact_merged_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            # The check fails whenever POISON.md exists in the checkout the
            # checks run in. It is absent from every candidate commit.
            data_dir, run_id, repository, _ = create_approved_run_with_check(
                temp_path, environment, _absent_file_check("POISON.md")
            )
            merged = merge_run(temp_path, data_dir, run_id, environment)
            self.assertEqual(merged.returncode, 0, merged.stderr)
            merged_sha = json.loads(merged.stdout)["merged_sha"]

            # The target branch moves on after the merge with a commit that
            # would fail the check. Verification must still pass because it
            # checks out the exact merged commit, not the branch head.
            (repository / "POISON.md").write_text("poison\n", encoding="utf-8")
            subprocess.run(["git", "add", "POISON.md"], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Poison the branch head"],
                cwd=repository,
                check=True,
                capture_output=True,
            )

            verified = verify_merge(temp_path, data_dir, run_id, environment)

            self.assertEqual(verified.returncode, 0, verified.stderr)
            response = json.loads(verified.stdout)
            self.assertTrue(response["passed"])
            self.assertEqual(response["state"], "merged")
            self.assertEqual(response["merged_sha"], merged_sha)
            evidence = json.loads(
                (data_dir / "runs" / run_id / "post-merge-verification.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(evidence["merged_sha"], merged_sha)
            self.assertTrue(evidence["passed"])
            self.assertTrue(evidence["checkout_clean"])
            self.assertEqual(evidence["verified_by"], HUMAN)
            self.assertTrue(evidence["checks"])
            self.assertTrue(
                all(check["returncode"] == 0 for check in evidence["checks"])
            )
            events = read_events(data_dir, run_id)
            verified_event = next(
                event
                for event in events
                if event["type"] == "post_merge_verified"
            )
            self.assertEqual(verified_event["merged_sha"], merged_sha)
            self.assertEqual(verified_event["verified_by"], HUMAN)
            status = agentflow(
                "status",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(json.loads(status.stdout)["state"], "merged")
            # The isolated checkout is temporary evidence-gathering machinery,
            # removed after the checks.
            self.assertFalse((data_dir / "verifications" / run_id).exists())

            # Verification evidence is write-once: a second verification is
            # refused with recorded evidence.
            again = verify_merge(temp_path, data_dir, run_id, environment)
            self.assertNotEqual(again.returncode, 0)
            self.assertIn("already recorded", again.stderr)
            events = read_events(data_dir, run_id)
            refusal = next(
                event for event in events if event["type"] == "post_merge_refused"
            )
            self.assertIn("already recorded", refusal["reason"])

    def test_passing_verification_allows_subsequent_merges(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            check = "python3 -c \"print('checks passed')\""
            data_dir, run_a, repository, _ = create_approved_run_with_check(
                temp_path, environment, check
            )
            merged = merge_run(temp_path, data_dir, run_a, environment)
            self.assertEqual(merged.returncode, 0, merged.stderr)

            verified = verify_merge(temp_path, data_dir, run_a, environment)
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertTrue(json.loads(verified.stdout)["passed"])

            run_b = drive_second_run_to_approved(
                temp_path, repository, data_dir, environment, check
            )
            merged_b = merge_run(temp_path, data_dir, run_b, environment)
            self.assertEqual(merged_b.returncode, 0, merged_b.stderr)
            self.assertEqual(
                json.loads(merged_b.stdout)["merged_sha"],
                repository_head(repository),
            )

    def test_failed_verification_blocks_shipping_until_human_resolution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            # An external switch: the check fails once this file exists, so
            # the checks pass through every pre-merge stage and fail only at
            # post-merge verification.
            switch = temp_path / "fail-post-merge-switch"
            check = _absent_file_check(str(switch))
            data_dir, run_a, repository, _ = create_approved_run_with_check(
                temp_path, environment, check
            )
            merged = merge_run(temp_path, data_dir, run_a, environment)
            self.assertEqual(merged.returncode, 0, merged.stderr)
            merged_sha = json.loads(merged.stdout)["merged_sha"]

            # A second Run is approved before the failure surfaces.
            run_b = drive_second_run_to_approved(
                temp_path, repository, data_dir, environment, check
            )
            head_before = repository_head(repository)

            switch.write_text("fail\n", encoding="utf-8")
            failed = verify_merge(temp_path, data_dir, run_a, environment)

            self.assertEqual(failed.returncode, 0, failed.stderr)
            response = json.loads(failed.stdout)
            self.assertFalse(response["passed"])
            self.assertEqual(response["state"], "merge_failed")
            proposal_id = response["recovery_proposal_id"]

            # The failure produced a Recovery Proposal record: evidence refs,
            # the merged SHA, and proposed options, requiring human review.
            proposal = json.loads(
                (
                    data_dir
                    / "proposals"
                    / proposal_id
                    / "recovery-proposal.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(proposal["kind"], "post_merge_failure")
            self.assertEqual(proposal["run_id"], run_a)
            self.assertEqual(proposal["merged_sha"], merged_sha)
            self.assertTrue(proposal["requires_human_review"])
            self.assertTrue(proposal["failed_checks"])
            self.assertEqual(
                {option["kind"] for option in proposal["options"]},
                {"revert", "forward_fix"},
            )
            self.assertEqual(
                Path(proposal["evidence"][0]["artifact"]).resolve(),
                (
                    data_dir / "runs" / run_a / "post-merge-verification.json"
                ).resolve(),
            )
            listed = agentflow(
                "recovery",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            entries = json.loads(listed.stdout)
            self.assertEqual(entries[0]["proposal_id"], proposal_id)
            self.assertEqual(entries[0]["state"], "proposed")

            # The proposal never auto-executes: the Target Repository is
            # untouched by the failure and its recording.
            self.assertEqual(repository_head(repository), head_before)
            self.assertTrue(_repository_clean(repository))

            # Shipping stop: further merges into the repository are refused
            # with evidence while the failure is unresolved.
            blocked = merge_run(temp_path, data_dir, run_b, environment)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("shipping is blocked", blocked.stderr)
            self.assertEqual(repository_head(repository), head_before)
            self.assertFalse((data_dir / "runs" / run_b / "merge.json").exists())
            refusal = next(
                event
                for event in read_events(data_dir, run_b)
                if event["type"] == "merge_refused"
            )
            self.assertIn("shipping is blocked", refusal["reason"])
            self.assertEqual(refusal["blocked_by_runs"], [run_a])

            # The unresolved failure cannot be abandoned away.
            abandoned = agentflow(
                "abandon",
                run_a,
                "--abandoned-by",
                HUMAN,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertNotEqual(abandoned.returncode, 0)
            self.assertIn("merge_failed", abandoned.stderr)

            # Only an attributed human resolution lifts the block.
            resolved = agentflow(
                "resolve-merge",
                run_a,
                "--resolved-by",
                HUMAN,
                "--resolution",
                "Reverted the merged commit manually and audited the branch",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            self.assertEqual(json.loads(resolved.stdout)["state"], "merged")
            resolution = json.loads(
                (data_dir / "proposals" / proposal_id / "resolution.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(resolution["resolved_by"], HUMAN)
            self.assertEqual(resolution["run_id"], run_a)
            resolved_event = next(
                event
                for event in read_events(data_dir, run_a)
                if event["type"] == "post_merge_resolved"
            )
            self.assertEqual(resolved_event["resolved_by"], HUMAN)
            self.assertEqual(resolved_event["recovery_proposal_id"], proposal_id)
            listed = agentflow(
                "recovery",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(json.loads(listed.stdout)[0]["state"], "resolved")
            # Resolution recorded evidence only; it executed nothing.
            self.assertEqual(repository_head(repository), head_before)
            self.assertTrue(_repository_clean(repository))

            # A second resolution is refused: the failure is already resolved.
            again = agentflow(
                "resolve-merge",
                run_a,
                "--resolved-by",
                HUMAN,
                "--resolution",
                "duplicate",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertNotEqual(again.returncode, 0)

            # The block is lifted: the pending merge now completes.
            merged_b = merge_run(temp_path, data_dir, run_b, environment)
            self.assertEqual(merged_b.returncode, 0, merged_b.stderr)
            self.assertEqual(json.loads(merged_b.stdout)["state"], "merged")

    def test_verification_refused_without_a_completed_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id, _, _ = create_approved_run(temp_path, environment)

            verified = verify_merge(temp_path, data_dir, run_id, environment)

            self.assertNotEqual(verified.returncode, 0)
            self.assertIn("cannot verify from state human_approved", verified.stderr)
            self.assertFalse(
                (data_dir / "runs" / run_id / "post-merge-verification.json")
                .exists()
            )
            events = read_events(data_dir, run_id)
            refusal = next(
                event for event in events if event["type"] == "post_merge_refused"
            )
            self.assertIn("cannot verify", refusal["reason"])
            self.assertFalse(
                any(event["type"] == "post_merge_verified" for event in events)
            )

    def test_post_merge_writer_cannot_append_approval_or_merge_events(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id, _, approved_sha = create_approved_run(
                temp_path, environment
            )
            events_before = read_events(data_dir, run_id)

            for event_type in (
                "human_approved",
                "merge_completed",
                "build_ready",
            ):
                with self.assertRaises(ValueError):
                    post_merge.append_post_merge_event(
                        data_dir=data_dir,
                        run_id=run_id,
                        event_type=event_type,
                        holder="post-merge-test",
                        approved_sha=approved_sha,
                    )

            self.assertEqual(read_events(data_dir, run_id), events_before)
            # Neither the approval command nor the merge command is reachable
            # from this module.
            self.assertFalse(hasattr(post_merge, "approve_run"))
            self.assertFalse(hasattr(post_merge, "merge_approved_run"))


if __name__ == "__main__":
    unittest.main()
