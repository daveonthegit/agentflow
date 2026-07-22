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

from agentflow.external_validation import (  # noqa: E402
    ExternalValidationError,
    list_external_validations,
    read_external_status,
    register_external_validation,
    validate_external_task,
)
from agentflow.run_kernel import (  # noqa: E402
    acquire_claim,
    default_claim_holder,
    list_runs,
)

try:
    from tests.test_advance_command import agentflow
except ImportError:  # unittest discover imports test modules without a package
    from test_advance_command import agentflow


PASSING_CHECK = "python3 -c \"print('ok')\""
FAILING_CHECK = "python3 -c \"import sys; sys.exit(1)\""


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def make_repo_and_worktree(
    temp_path: Path,
    environment: dict[str, str],
    *,
    check: str = PASSING_CHECK,
    profile: bool = True,
    extra_files: dict[str, str] | None = None,
) -> tuple[Path, Path, str]:
    """Create a target repository, a profile commit, and a caller-owned worktree.

    Returns the repository path, the worktree path, and the worktree HEAD SHA.
    Agentflow never creates this worktree — the caller (here, the test) does,
    mirroring how Firstmate owns worktree lifecycle.
    """
    repository = temp_path / "target"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "agentflow@example.test", cwd=repository)
    _git("config", "user.name", "Agentflow Test", cwd=repository)
    (repository / "README.md").write_text("# Target\n", encoding="utf-8")
    for relative, content in (extra_files or {}).items():
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git("add", "--all", cwd=repository)
    _git("commit", "-m", "Initial commit", cwd=repository)
    if profile:
        profiled = agentflow(
            "profile",
            "--check",
            check,
            "--test-path",
            "tests",
            cwd=repository,
            environment=environment,
        )
        if profiled.returncode != 0:
            raise AssertionError(profiled.stderr)
        _git("add", "-f", ".agentflow/repository-profile.json", cwd=repository)
        _git("commit", "-m", "Add repository profile", cwd=repository)
    worktree = temp_path / "candidate-worktree"
    _git("worktree", "add", str(worktree), "HEAD", cwd=repository)
    sha = _git("rev-parse", "HEAD", cwd=worktree)
    return repository, worktree, sha


def read_events(data_dir: Path, external_id: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (data_dir / "external" / external_id / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


class ExternalValidationHappyPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.temp_path = Path(self._temp.name)
        self.environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
        self.data_dir = self.temp_path / "home"

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_register_then_validate_passes_and_records_replayable_evidence(
        self,
    ) -> None:
        _, worktree, sha = make_repo_and_worktree(self.temp_path, self.environment)
        registered = register_external_validation(
            summary="Validate an externally built candidate",
            worktree=worktree,
            candidate_sha=sha,
            acceptance_criteria=["the authoritative checks pass"],
            external_ref="firstmate-task-42",
            data_dir=self.data_dir,
        )
        self.assertEqual(registered.state, "registered")
        self.assertEqual(registered.candidate_sha, sha)
        self.assertEqual(registered.external_ref, "firstmate-task-42")

        result = validate_external_task(
            external_id=registered.external_id,
            validated_by="firstmate",
            data_dir=self.data_dir,
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.state, "validated")
        self.assertEqual(result.candidate_sha, sha)
        self.assertTrue(result.artifact.exists())

        # Evidence replays to the same outcome and candidate identity in a fresh
        # read, and carries the attribution.
        status = read_external_status(
            external_id=registered.external_id, data_dir=self.data_dir
        )
        self.assertEqual(status.state, "validated")
        self.assertEqual(status.candidate_sha, sha)
        self.assertEqual(status.validated_by, "firstmate")
        self.assertEqual(status.external_ref, "firstmate-task-42")

        # The checks artifact preserves the deterministic evidence quality of
        # the built-stage checks: per-check environment fingerprint, timing,
        # and raw output.
        checks = json.loads(result.artifact.read_text(encoding="utf-8"))
        self.assertEqual(checks["candidate_sha"], sha)
        self.assertTrue(checks["workspace_clean"])
        self.assertEqual(len(checks["checks"]), 1)
        record = checks["checks"][0]
        for field in ("started_at", "duration_ms", "returncode", "environment"):
            self.assertIn(field, record)
        self.assertEqual(record["environment"]["TZ"], "UTC")

        # Events are append-only with contiguous one-based sequence numbers.
        events = read_events(self.data_dir, registered.external_id)
        self.assertEqual(
            [event["sequence"] for event in events],
            list(range(1, len(events) + 1)),
        )

    def test_failing_checks_reach_validation_failed(self) -> None:
        _, worktree, sha = make_repo_and_worktree(
            self.temp_path, self.environment, check=FAILING_CHECK
        )
        registered = register_external_validation(
            summary="Candidate whose checks fail",
            worktree=worktree,
            candidate_sha=sha,
            data_dir=self.data_dir,
        )
        result = validate_external_task(
            external_id=registered.external_id, data_dir=self.data_dir
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.state, "validation_failed")
        status = read_external_status(
            external_id=registered.external_id, data_dir=self.data_dir
        )
        self.assertEqual(status.state, "validation_failed")

    def test_abbreviated_candidate_sha_is_accepted_and_resolved(self) -> None:
        _, worktree, sha = make_repo_and_worktree(self.temp_path, self.environment)
        registered = register_external_validation(
            summary="Register with a short SHA",
            worktree=worktree,
            candidate_sha=sha[:10],
            data_dir=self.data_dir,
        )
        self.assertEqual(registered.candidate_sha, sha)

    def test_list_reports_registered_validations(self) -> None:
        _, worktree, sha = make_repo_and_worktree(self.temp_path, self.environment)
        registered = register_external_validation(
            summary="A registered validation",
            worktree=worktree,
            candidate_sha=sha,
            data_dir=self.data_dir,
        )
        listed = list_external_validations(data_dir=self.data_dir)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].external_id, registered.external_id)
        self.assertEqual(listed[0].state, "registered")


class ExternalValidationRejectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.temp_path = Path(self._temp.name)
        self.environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
        self.data_dir = self.temp_path / "home"

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_missing_profile_is_rejected_at_registration(self) -> None:
        _, worktree, sha = make_repo_and_worktree(
            self.temp_path, self.environment, profile=False
        )
        with self.assertRaises(ExternalValidationError) as context:
            register_external_validation(
                summary="No profile committed",
                worktree=worktree,
                candidate_sha=sha,
                data_dir=self.data_dir,
            )
        self.assertIn("missing Repository Profile", str(context.exception))
        self.assertFalse((self.data_dir / "external").exists())

    def test_stale_profile_is_rejected_at_registration(self) -> None:
        _, worktree, _ = make_repo_and_worktree(self.temp_path, self.environment)
        # Commit a source change on top of the profiled candidate so the
        # committed profile's fingerprint no longer matches the worktree.
        (worktree / "CHANGELOG.md").write_text("drift\n", encoding="utf-8")
        _git("add", "--all", cwd=worktree)
        _git("commit", "-m", "Drift after profiling", cwd=worktree)
        stale_sha = _git("rev-parse", "HEAD", cwd=worktree)
        with self.assertRaises(ExternalValidationError) as context:
            register_external_validation(
                summary="Profile is stale",
                worktree=worktree,
                candidate_sha=stale_sha,
                data_dir=self.data_dir,
            )
        self.assertIn("stale Repository Profile", str(context.exception))

    def test_dirty_worktree_is_rejected_at_registration(self) -> None:
        _, worktree, sha = make_repo_and_worktree(self.temp_path, self.environment)
        (worktree / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")
        with self.assertRaises(ExternalValidationError) as context:
            register_external_validation(
                summary="Dirty worktree",
                worktree=worktree,
                candidate_sha=sha,
                data_dir=self.data_dir,
            )
        self.assertIn("dirty worktree", str(context.exception))

    def test_mismatched_sha_is_rejected_at_registration(self) -> None:
        _, worktree, _ = make_repo_and_worktree(self.temp_path, self.environment)
        with self.assertRaises(ExternalValidationError) as context:
            register_external_validation(
                summary="SHA is not present",
                worktree=worktree,
                candidate_sha="0" * 40,
                data_dir=self.data_dir,
            )
        self.assertIn("mismatched SHA", str(context.exception))

    def test_invalid_worktree_path_is_rejected(self) -> None:
        with self.assertRaises(ExternalValidationError) as context:
            register_external_validation(
                summary="No such worktree",
                worktree=self.temp_path / "does-not-exist",
                candidate_sha="0" * 40,
                data_dir=self.data_dir,
            )
        self.assertIn("invalid worktree path", str(context.exception))

    def test_worktree_not_belonging_to_repository_is_rejected(self) -> None:
        _, worktree, sha = make_repo_and_worktree(self.temp_path, self.environment)
        other = self.temp_path / "unrelated"
        other.mkdir()
        _git("init", cwd=other)
        _git("config", "user.email", "x@example.test", cwd=other)
        _git("config", "user.name", "X", cwd=other)
        (other / "a.txt").write_text("a\n", encoding="utf-8")
        _git("add", "--all", cwd=other)
        _git("commit", "-m", "unrelated", cwd=other)
        with self.assertRaises(ExternalValidationError) as context:
            register_external_validation(
                summary="Mismatched repository",
                worktree=worktree,
                candidate_sha=sha,
                repository=other,
                data_dir=self.data_dir,
            )
        self.assertIn("does not belong to repository", str(context.exception))

    def test_candidate_moving_after_registration_refuses_validation(self) -> None:
        _, worktree, sha = make_repo_and_worktree(self.temp_path, self.environment)
        registered = register_external_validation(
            summary="Candidate moves under us",
            worktree=worktree,
            candidate_sha=sha,
            data_dir=self.data_dir,
        )
        # The caller advances the worktree to a new commit after registering.
        (worktree / "later.txt").write_text("later\n", encoding="utf-8")
        _git("add", "--all", cwd=worktree)
        _git("commit", "-m", "Move HEAD after registration", cwd=worktree)
        with self.assertRaises(ExternalValidationError) as context:
            validate_external_task(
                external_id=registered.external_id, data_dir=self.data_dir
            )
        self.assertIn("mismatched SHA", str(context.exception))
        # The refusal is recorded as evidence and state stays registered so the
        # caller can restore the candidate and re-validate.
        status = read_external_status(
            external_id=registered.external_id, data_dir=self.data_dir
        )
        self.assertEqual(status.state, "registered")
        events = read_events(self.data_dir, registered.external_id)
        self.assertTrue(
            any(e["type"] == "external_validation_refused" for e in events)
        )

    def test_dirty_worktree_refuses_validation_but_allows_retry(self) -> None:
        _, worktree, sha = make_repo_and_worktree(self.temp_path, self.environment)
        registered = register_external_validation(
            summary="Dirty at validation time",
            worktree=worktree,
            candidate_sha=sha,
            data_dir=self.data_dir,
        )
        scratch = worktree / "scratch.txt"
        scratch.write_text("uncommitted\n", encoding="utf-8")
        with self.assertRaises(ExternalValidationError):
            validate_external_task(
                external_id=registered.external_id, data_dir=self.data_dir
            )
        self.assertEqual(
            read_external_status(
                external_id=registered.external_id, data_dir=self.data_dir
            ).state,
            "registered",
        )
        # Cleaning up the worktree lets the same registration validate.
        scratch.unlink()
        result = validate_external_task(
            external_id=registered.external_id, data_dir=self.data_dir
        )
        self.assertTrue(result.passed)

    def test_revalidating_terminal_validation_is_refused(self) -> None:
        _, worktree, sha = make_repo_and_worktree(self.temp_path, self.environment)
        registered = register_external_validation(
            summary="Only validate once",
            worktree=worktree,
            candidate_sha=sha,
            data_dir=self.data_dir,
        )
        validate_external_task(
            external_id=registered.external_id, data_dir=self.data_dir
        )
        with self.assertRaises(ExternalValidationError) as context:
            validate_external_task(
                external_id=registered.external_id, data_dir=self.data_dir
            )
        self.assertIn("cannot validate from state validated", str(context.exception))

    def test_unknown_id_is_rejected(self) -> None:
        with self.assertRaises(ExternalValidationError):
            validate_external_task(external_id="deadbeef", data_dir=self.data_dir)
        with self.assertRaises(ExternalValidationError):
            read_external_status(external_id="deadbeef", data_dir=self.data_dir)


class ExternalValidationBoundaryTests(unittest.TestCase):
    """Assert the architectural boundary the integration must never cross."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.temp_path = Path(self._temp.name)
        self.environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
        self.data_dir = self.temp_path / "home"

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_external_path_is_isolated_from_the_run_store(self) -> None:
        _, worktree, sha = make_repo_and_worktree(self.temp_path, self.environment)
        registered = register_external_validation(
            summary="Isolated from runs",
            worktree=worktree,
            candidate_sha=sha,
            data_dir=self.data_dir,
        )
        validate_external_task(
            external_id=registered.external_id, data_dir=self.data_dir
        )
        # No Run is ever created: the external path shares no store with runs/.
        self.assertEqual(list_runs(data_dir=self.data_dir), [])
        self.assertFalse((self.data_dir / "runs").exists())
        self.assertTrue(
            (self.data_dir / "external" / registered.external_id).is_dir()
        )
        # Agentflow never creates a worktree: only the caller's worktree exists.
        self.assertFalse((self.data_dir / "worktrees").exists())

    def test_event_vocabulary_never_reaches_a_human_or_delivery_gate(self) -> None:
        _, worktree, sha = make_repo_and_worktree(self.temp_path, self.environment)
        registered = register_external_validation(
            summary="No approval or merge events",
            worktree=worktree,
            candidate_sha=sha,
            data_dir=self.data_dir,
        )
        validate_external_task(
            external_id=registered.external_id, data_dir=self.data_dir
        )
        events = read_events(self.data_dir, registered.external_id)
        types = {event["type"] for event in events}
        forbidden = {
            "awaiting_human",
            "human_approved",
            "human_rejected",
            "review_ready",
            "review_blocked",
            "build_ready",
            "merge_completed",
            "merge_refused",
            "deployment_completed",
        }
        self.assertEqual(types & forbidden, set())
        # Every external event uses the external vocabulary or shared claim
        # bookkeeping — nothing from the Run workflow.
        allowed = {
            "external_registered",
            "external_candidate_identified",
            "external_profile_captured",
            "external_checks_passed",
            "external_checks_failed",
            "external_validation_refused",
            "claim_acquired",
            "claim_released",
            "claim_expired",
        }
        self.assertTrue(types <= allowed, types - allowed)
        # The terminal state is a plain validation outcome, never a human gate.
        status = read_external_status(
            external_id=registered.external_id, data_dir=self.data_dir
        )
        self.assertIn(status.state, {"validated", "validation_failed"})

    def test_concurrent_validation_is_blocked_by_the_stage_claim(self) -> None:
        _, worktree, sha = make_repo_and_worktree(self.temp_path, self.environment)
        registered = register_external_validation(
            summary="Claim-guarded validation",
            worktree=worktree,
            candidate_sha=sha,
            data_dir=self.data_dir,
        )
        events_path = (
            self.data_dir / "external" / registered.external_id / "events.jsonl"
        )
        # Simulate another live process holding the stage claim.
        acquire_claim(
            data_dir=self.data_dir,
            run_id=registered.external_id,
            holder="other-host:999",
            events_path=events_path,
        )
        with self.assertRaises(ValueError) as context:
            validate_external_task(
                external_id=registered.external_id, data_dir=self.data_dir
            )
        self.assertIn("already claimed", str(context.exception))


class ExternalValidationCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.temp_path = Path(self._temp.name)
        self.environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
        self.data_dir = self.temp_path / "home"

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_cli_register_validate_status_contract(self) -> None:
        repository, worktree, sha = make_repo_and_worktree(
            self.temp_path, self.environment
        )
        registered = agentflow(
            "external",
            "register",
            "Validate via the CLI",
            "--worktree",
            str(worktree),
            "--candidate-sha",
            sha,
            "--repository",
            str(repository),
            "--external-ref",
            "fm-7",
            "--data-dir",
            str(self.data_dir),
            cwd=self.temp_path,
            environment=self.environment,
        )
        self.assertEqual(registered.returncode, 0, registered.stderr)
        external_id = json.loads(registered.stdout)["external_id"]

        validated = agentflow(
            "external",
            "validate",
            external_id,
            "--validated-by",
            "firstmate",
            "--data-dir",
            str(self.data_dir),
            cwd=self.temp_path,
            environment=self.environment,
        )
        self.assertEqual(validated.returncode, 0, validated.stderr)
        payload = json.loads(validated.stdout)
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["state"], "validated")

        status = agentflow(
            "external",
            "status",
            external_id,
            "--data-dir",
            str(self.data_dir),
            cwd=self.temp_path,
            environment=self.environment,
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        status_payload = json.loads(status.stdout)
        self.assertEqual(status_payload["state"], "validated")
        self.assertEqual(status_payload["candidate_sha"], sha)
        self.assertEqual(status_payload["external_ref"], "fm-7")

    def test_cli_validate_exits_nonzero_on_failed_checks(self) -> None:
        _, worktree, sha = make_repo_and_worktree(
            self.temp_path, self.environment, check=FAILING_CHECK
        )
        registered = agentflow(
            "external",
            "register",
            "Failing checks via CLI",
            "--worktree",
            str(worktree),
            "--candidate-sha",
            sha,
            "--data-dir",
            str(self.data_dir),
            cwd=self.temp_path,
            environment=self.environment,
        )
        external_id = json.loads(registered.stdout)["external_id"]
        validated = agentflow(
            "external",
            "validate",
            external_id,
            "--data-dir",
            str(self.data_dir),
            cwd=self.temp_path,
            environment=self.environment,
        )
        self.assertEqual(validated.returncode, 1)
        self.assertEqual(json.loads(validated.stdout)["state"], "validation_failed")

    def test_cli_register_reports_clean_error_for_missing_profile(self) -> None:
        _, worktree, sha = make_repo_and_worktree(
            self.temp_path, self.environment, profile=False
        )
        registered = agentflow(
            "external",
            "register",
            "No profile",
            "--worktree",
            str(worktree),
            "--candidate-sha",
            sha,
            "--data-dir",
            str(self.data_dir),
            cwd=self.temp_path,
            environment=self.environment,
        )
        self.assertEqual(registered.returncode, 1)
        self.assertIn("missing Repository Profile", registered.stderr)
        self.assertEqual(registered.stdout, "")


if __name__ == "__main__":
    unittest.main()
