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

import agentflow.deployment as deployment  # noqa: E402
from agentflow.run_kernel import STATE_BY_EVENT  # noqa: E402

try:
    from tests.test_advance_command import agentflow
    from tests.test_merge_command import (
        DEFAULT_CHECK,
        create_approved_run,
        read_events,
        repository_head,
    )
    from tests.test_post_merge_verification import (
        create_approved_run_with_check,
        drive_second_run_to_approved,
        merge_run,
        verify_merge,
    )
except ImportError:  # unittest discover imports test modules without a package
    from test_advance_command import agentflow
    from test_merge_command import (
        DEFAULT_CHECK,
        create_approved_run,
        read_events,
        repository_head,
    )
    from test_post_merge_verification import (
        create_approved_run_with_check,
        drive_second_run_to_approved,
        merge_run,
        verify_merge,
    )


HUMAN = "deploy-test-human"


def configure_deployment(
    repository: Path,
    environment: dict[str, str],
    *deploy_arguments: str,
    check: str = DEFAULT_CHECK,
) -> None:
    """Record a deployment configuration in the committed Repository Profile."""
    profiled = agentflow(
        "profile",
        "--check",
        check,
        "--test-path",
        "tests",
        "--allow-merge",
        *deploy_arguments,
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
        ["git", "commit", "-m", "Record deployment configuration"],
        cwd=repository,
        check=True,
        capture_output=True,
    )


def create_verified_merged_run(
    temp_path: Path,
    environment: dict[str, str],
    *,
    check: str = DEFAULT_CHECK,
) -> tuple[Path, str, Path, str]:
    """Drive a Run to merged with passing Post-Merge Verification.

    Returns the data dir, run id, Target Repository, and merged SHA.
    """
    data_dir, run_id, repository, _ = create_approved_run(
        temp_path, environment, check=check
    )
    merged = merge_run(temp_path, data_dir, run_id, environment)
    if merged.returncode != 0:
        raise AssertionError(merged.stderr)
    merged_sha = json.loads(merged.stdout)["merged_sha"]
    verified = verify_merge(temp_path, data_dir, run_id, environment)
    if verified.returncode != 0:
        raise AssertionError(verified.stderr)
    if not json.loads(verified.stdout)["passed"]:
        raise AssertionError(verified.stdout)
    return data_dir, run_id, repository, merged_sha


def deploy(
    temp_path: Path,
    data_dir: Path,
    run_id: str,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return agentflow(
        "deploy",
        run_id,
        "--deployed-by",
        HUMAN,
        "--data-dir",
        str(data_dir),
        cwd=temp_path,
        environment=environment,
    )


def run_state(
    temp_path: Path,
    data_dir: Path,
    run_id: str,
    environment: dict[str, str],
) -> str:
    status = agentflow(
        "status",
        run_id,
        "--data-dir",
        str(data_dir),
        cwd=temp_path,
        environment=environment,
    )
    return json.loads(status.stdout)["state"]


class DeployCommandTests(unittest.TestCase):
    def test_deploy_ships_exactly_the_merged_sha_with_write_once_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id, repository, merged_sha = (
                create_verified_merged_run(temp_path, environment)
            )
            target = temp_path / "deploy-target"
            configure_deployment(
                repository,
                environment,
                "--deploy-adapter",
                "directory",
                "--deploy-target",
                str(target),
            )
            # The target branch moves on after verification with a commit the
            # adapter must never ship: deployment checks out the exact
            # merged_sha, not the branch head.
            (repository / "POISON.md").write_text("poison\n", encoding="utf-8")
            subprocess.run(["git", "add", "POISON.md"], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Poison the branch head"],
                cwd=repository,
                check=True,
                capture_output=True,
            )
            self.assertNotEqual(repository_head(repository), merged_sha)

            deployed = deploy(temp_path, data_dir, run_id, environment)

            self.assertEqual(deployed.returncode, 0, deployed.stderr)
            response = json.loads(deployed.stdout)
            self.assertEqual(response["adapter"], "directory")
            self.assertEqual(response["deployed_by"], HUMAN)
            self.assertEqual(response["merged_sha"], merged_sha)
            # Deployment changes no workflow state semantics: the Run's
            # replayed state stays merged (still delivered).
            self.assertEqual(response["state"], "merged")
            self.assertEqual(
                run_state(temp_path, data_dir, run_id, environment), "merged"
            )

            # The published content is exactly the merged revision: the
            # candidate's README, the recorded revision marker, and nothing
            # from the moved-on branch head.
            self.assertIn(
                "Health endpoint documented",
                (target / "README.md").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (target / ".agentflow-deployed-revision")
                .read_text(encoding="utf-8")
                .strip(),
                merged_sha,
            )
            self.assertFalse((target / "POISON.md").exists())
            self.assertFalse((target / ".git").exists())
            # The isolated checkout is temporary machinery, removed after.
            self.assertFalse((data_dir / "deployments" / run_id).exists())

            evidence = json.loads(
                (data_dir / "runs" / run_id / "deployment.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(evidence["adapter"], "directory")
            self.assertEqual(evidence["deployed_by"], HUMAN)
            self.assertEqual(evidence["merged_sha"], merged_sha)
            self.assertEqual(evidence["run_id"], run_id)
            self.assertEqual(evidence["repository"], str(repository))
            self.assertTrue(evidence["steps"])
            self.assertTrue(all(step["ok"] for step in evidence["steps"]))
            attempt = json.loads(
                Path(response["attempt_artifact"]).read_text(encoding="utf-8")
            )
            self.assertTrue(attempt["passed"])
            self.assertEqual(attempt["merged_sha"], merged_sha)

            events = read_events(data_dir, run_id)
            completed = next(
                event
                for event in events
                if event["type"] == "deployment_completed"
            )
            self.assertEqual(completed["merged_sha"], merged_sha)
            self.assertEqual(completed["deployed_by"], HUMAN)
            self.assertEqual(completed["adapter"], "directory")
            # Deployment granted no approval and recorded no merge: the log
            # still carries exactly one human approval and one merge.
            self.assertEqual(
                sum(1 for event in events if event["type"] == "human_approved"),
                1,
            )
            self.assertEqual(
                sum(1 for event in events if event["type"] == "merge_completed"),
                1,
            )

            # Deployment evidence is write-once: a second deploy is
            # deterministically refused with recorded evidence.
            evidence_before = (
                data_dir / "runs" / run_id / "deployment.json"
            ).read_text(encoding="utf-8")
            again = deploy(temp_path, data_dir, run_id, environment)
            self.assertNotEqual(again.returncode, 0)
            self.assertIn("already recorded", again.stderr)
            self.assertEqual(
                (data_dir / "runs" / run_id / "deployment.json").read_text(
                    encoding="utf-8"
                ),
                evidence_before,
            )
            refusal = next(
                event
                for event in read_events(data_dir, run_id)
                if event["type"] == "deployment_refused"
            )
            self.assertIn("already recorded", refusal["reason"])

    def test_deploy_refused_when_the_run_is_not_merged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id, repository, _ = create_approved_run(
                temp_path, environment
            )
            configure_deployment(
                repository,
                environment,
                "--deploy-adapter",
                "directory",
                "--deploy-target",
                str(temp_path / "deploy-target"),
            )

            deployed = deploy(temp_path, data_dir, run_id, environment)

            self.assertNotEqual(deployed.returncode, 0)
            self.assertIn("cannot deploy from state human_approved", deployed.stderr)
            self.assertFalse((temp_path / "deploy-target").exists())
            self.assertFalse(
                (data_dir / "runs" / run_id / "deployment.json").exists()
            )
            events = read_events(data_dir, run_id)
            refusal = next(
                event for event in events if event["type"] == "deployment_refused"
            )
            self.assertIn("cannot deploy", refusal["reason"])
            self.assertFalse(
                any(event["type"] == "deployment_completed" for event in events)
            )

    def test_deploy_refused_without_post_merge_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id, repository, _ = create_approved_run(
                temp_path, environment
            )
            merged = merge_run(temp_path, data_dir, run_id, environment)
            self.assertEqual(merged.returncode, 0, merged.stderr)
            configure_deployment(
                repository,
                environment,
                "--deploy-adapter",
                "directory",
                "--deploy-target",
                str(temp_path / "deploy-target"),
            )

            deployed = deploy(temp_path, data_dir, run_id, environment)

            self.assertNotEqual(deployed.returncode, 0)
            self.assertIn("no post-merge verification evidence", deployed.stderr)
            self.assertFalse((temp_path / "deploy-target").exists())
            self.assertFalse(
                (data_dir / "runs" / run_id / "deployment.json").exists()
            )
            refusal = next(
                event
                for event in read_events(data_dir, run_id)
                if event["type"] == "deployment_refused"
            )
            self.assertIn("verify-merge", refusal["reason"])

    def test_failed_verification_never_becomes_deployable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            # The check fails once this external switch file exists, so it
            # passes every pre-merge stage and fails only at verification.
            switch = temp_path / "fail-post-merge-switch"
            check = (
                'python3 -c "import pathlib, sys; '
                f"sys.exit(1 if pathlib.Path('{switch}').exists() else 0)\""
            )
            data_dir, run_id, repository, _ = create_approved_run_with_check(
                temp_path, environment, check
            )
            merged = merge_run(temp_path, data_dir, run_id, environment)
            self.assertEqual(merged.returncode, 0, merged.stderr)
            switch.write_text("fail\n", encoding="utf-8")
            failed = verify_merge(temp_path, data_dir, run_id, environment)
            self.assertEqual(failed.returncode, 0, failed.stderr)
            self.assertFalse(json.loads(failed.stdout)["passed"])
            switch.unlink()
            configure_deployment(
                repository,
                environment,
                "--deploy-adapter",
                "directory",
                "--deploy-target",
                str(temp_path / "deploy-target"),
                check=check,
            )

            # From merge_failed the state gate refuses.
            deployed = deploy(temp_path, data_dir, run_id, environment)
            self.assertNotEqual(deployed.returncode, 0)
            self.assertIn("cannot deploy from state merge_failed", deployed.stderr)

            # A human resolution lifts the shipping block for other work but
            # never makes the failed revision itself deployable.
            resolved = agentflow(
                "resolve-merge",
                run_id,
                "--resolved-by",
                HUMAN,
                "--resolution",
                "Reverted the merged commit manually",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            self.assertEqual(
                run_state(temp_path, data_dir, run_id, environment), "merged"
            )

            deployed = deploy(temp_path, data_dir, run_id, environment)
            self.assertNotEqual(deployed.returncode, 0)
            self.assertIn("did not pass", deployed.stderr)
            self.assertFalse((temp_path / "deploy-target").exists())
            self.assertFalse(
                (data_dir / "runs" / run_id / "deployment.json").exists()
            )
            events = read_events(data_dir, run_id)
            self.assertIn(
                "never deployable",
                [
                    event["reason"]
                    for event in events
                    if event["type"] == "deployment_refused"
                ][-1],
            )
            self.assertFalse(
                any(event["type"] == "deployment_completed" for event in events)
            )

    def test_deploy_refused_while_shipping_stop_is_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            switch = temp_path / "fail-post-merge-switch"
            check = (
                'python3 -c "import pathlib, sys; '
                f"sys.exit(1 if pathlib.Path('{switch}').exists() else 0)\""
            )
            # Run A merges and verifies cleanly: it is deployable on its own.
            data_dir, run_a, repository, merged_sha_a = (
                create_verified_merged_run(temp_path, environment, check=check)
            )
            # Run B merges, then its verification fails: an unresolved
            # shipping stop for the whole Target Repository.
            run_b = drive_second_run_to_approved(
                temp_path, repository, data_dir, environment, check
            )
            merged_b = merge_run(temp_path, data_dir, run_b, environment)
            self.assertEqual(merged_b.returncode, 0, merged_b.stderr)
            switch.write_text("fail\n", encoding="utf-8")
            failed = verify_merge(temp_path, data_dir, run_b, environment)
            self.assertEqual(failed.returncode, 0, failed.stderr)
            self.assertFalse(json.loads(failed.stdout)["passed"])
            switch.unlink()
            target = temp_path / "deploy-target"
            configure_deployment(
                repository,
                environment,
                "--deploy-adapter",
                "directory",
                "--deploy-target",
                str(target),
                check=check,
            )

            blocked = deploy(temp_path, data_dir, run_a, environment)

            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("shipping is blocked", blocked.stderr)
            self.assertFalse(target.exists())
            self.assertFalse(
                (data_dir / "runs" / run_a / "deployment.json").exists()
            )
            refusal = next(
                event
                for event in read_events(data_dir, run_a)
                if event["type"] == "deployment_refused"
            )
            self.assertIn("shipping is blocked", refusal["reason"])
            self.assertEqual(refusal["blocked_by_runs"], [run_b])

            # Only an attributed human resolution lifts the block; the
            # verified revision then ships.
            resolved = agentflow(
                "resolve-merge",
                run_b,
                "--resolved-by",
                HUMAN,
                "--resolution",
                "Reverted run B's merged commit manually",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            deployed = deploy(temp_path, data_dir, run_a, environment)
            self.assertEqual(deployed.returncode, 0, deployed.stderr)
            self.assertEqual(
                json.loads(deployed.stdout)["merged_sha"], merged_sha_a
            )
            self.assertEqual(
                (target / ".agentflow-deployed-revision")
                .read_text(encoding="utf-8")
                .strip(),
                merged_sha_a,
            )

    def test_deploy_refused_without_deployment_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id, _, _ = create_verified_merged_run(
                temp_path, environment
            )

            deployed = deploy(temp_path, data_dir, run_id, environment)

            self.assertNotEqual(deployed.returncode, 0)
            self.assertIn("no deployment configuration", deployed.stderr)
            self.assertFalse(
                (data_dir / "runs" / run_id / "deployment.json").exists()
            )
            refusal = next(
                event
                for event in read_events(data_dir, run_id)
                if event["type"] == "deployment_refused"
            )
            self.assertIn("deployment is not permitted", refusal["reason"])

    def test_command_adapter_runs_in_isolated_checkout_and_retries_after_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id, repository, merged_sha = (
                create_verified_merged_run(temp_path, environment)
            )
            run_dir = data_dir / "runs" / run_id

            # First attempt: the deploy command fails. The attempt is
            # recorded as evidence, refusal is recorded, and no write-once
            # deployment evidence exists — so a retry stays possible.
            configure_deployment(
                repository,
                environment,
                "--deploy-adapter",
                "command",
                "--deploy-command",
                'python3 -c "import sys; sys.exit(1)"',
            )
            failed = deploy(temp_path, data_dir, run_id, environment)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("deploy command failed", failed.stderr)
            self.assertFalse((run_dir / "deployment.json").exists())
            attempt_one = json.loads(
                (run_dir / "deployment-attempt-1.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(attempt_one["passed"])
            self.assertEqual(attempt_one["merged_sha"], merged_sha)
            refusal = next(
                event
                for event in read_events(data_dir, run_id)
                if event["type"] == "deployment_refused"
            )
            self.assertIn("deploy command failed", refusal["reason"])

            # Second attempt with a repaired command: it observes the exact
            # revision identity and runs inside the isolated checkout, never
            # a Run Workspace or the primary checkout.
            out_path = temp_path / "deploy-command-observed.txt"
            command = (
                'python3 -c "import os, pathlib; '
                f"pathlib.Path('{out_path}').write_text("
                "os.environ['AGENTFLOW_DEPLOY_REVISION'] + chr(10) + os.getcwd())\""
            )
            configure_deployment(
                repository,
                environment,
                "--deploy-adapter",
                "command",
                "--deploy-command",
                command,
            )
            deployed = deploy(temp_path, data_dir, run_id, environment)
            self.assertEqual(deployed.returncode, 0, deployed.stderr)
            response = json.loads(deployed.stdout)
            self.assertEqual(response["adapter"], "command")
            self.assertEqual(response["merged_sha"], merged_sha)
            observed_revision, observed_cwd = (
                out_path.read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(observed_revision, merged_sha)
            self.assertEqual(
                Path(observed_cwd).resolve(),
                (data_dir / "deployments" / run_id).resolve(),
            )
            self.assertNotEqual(Path(observed_cwd).resolve(), repository.resolve())
            # Earlier attempt evidence is never overwritten: the retry is the
            # next indexed artifact, referenced by write-once deployment.json.
            self.assertTrue((run_dir / "deployment-attempt-1.json").exists())
            evidence = json.loads(
                (run_dir / "deployment.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                Path(evidence["attempt_artifact"]).resolve(),
                (run_dir / "deployment-attempt-2.json").resolve(),
            )

    def test_directory_target_inside_the_repository_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id, repository, _ = create_verified_merged_run(
                temp_path, environment
            )
            configure_deployment(
                repository,
                environment,
                "--deploy-adapter",
                "directory",
                "--deploy-target",
                "dist",
            )

            deployed = deploy(temp_path, data_dir, run_id, environment)

            self.assertNotEqual(deployed.returncode, 0)
            self.assertIn("outside the Target Repository", deployed.stderr)
            self.assertFalse((repository / "dist").exists())

    def test_profile_deploy_flags_are_validated_together(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            for arguments, message in (
                (
                    ("--deploy-adapter", "directory"),
                    "requires --deploy-target",
                ),
                (
                    ("--deploy-adapter", "command"),
                    "requires --deploy-command",
                ),
                (
                    ("--deploy-target", "somewhere"),
                    "require --deploy-adapter",
                ),
                (
                    ("--deploy-command", "true"),
                    "require --deploy-adapter",
                ),
            ):
                result = agentflow(
                    "profile",
                    "--check",
                    DEFAULT_CHECK,
                    *arguments,
                    cwd=temp_path,
                    environment=environment,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_deployment_cannot_grant_approval_or_alter_workflow_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
            data_dir, run_id, _, approved_sha = create_approved_run(
                temp_path, environment
            )
            events_before = read_events(data_dir, run_id)

            # The deployment module's only event writer structurally refuses
            # approval, merge, verification, and every other foreign event
            # type before touching the log.
            for event_type in (
                "human_approved",
                "merge_completed",
                "post_merge_verified",
                "post_merge_resolved",
                "build_ready",
            ):
                with self.assertRaises(ValueError):
                    deployment.append_deployment_event(
                        data_dir=data_dir,
                        run_id=run_id,
                        event_type=event_type,
                        holder="deployment-test",
                        approved_sha=approved_sha,
                    )

            self.assertEqual(read_events(data_dir, run_id), events_before)
            # Neither the approval command, the merge command, nor the
            # post-merge verification/resolution commands are reachable from
            # the deployment module.
            self.assertFalse(hasattr(deployment, "approve_run"))
            self.assertFalse(hasattr(deployment, "merge_approved_run"))
            self.assertFalse(hasattr(deployment, "verify_merged_run"))
            self.assertFalse(hasattr(deployment, "resolve_post_merge_failure"))
            # Deployment events carry no state transition at all: like
            # merge_refused, they leave the replayed Run State unchanged.
            self.assertNotIn("deployment_completed", STATE_BY_EVENT)
            self.assertNotIn("deployment_refused", STATE_BY_EVENT)


if __name__ == "__main__":
    unittest.main()
