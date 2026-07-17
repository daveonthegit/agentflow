from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).parents[1]


def _run_init(repository: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agentflow", "init", *extra],
        cwd=repository,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )


def _git(repository: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )


def _init_git_repo(repository: Path) -> None:
    repository.mkdir(parents=True, exist_ok=True)
    _git(repository, "init")
    _git(repository, "config", "user.email", "agentflow@example.test")
    _git(repository, "config", "user.name", "Agentflow Test")
    _git(repository, "config", "commit.gpgsign", "false")
    (repository / "README.md").write_text("# Repo\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    committed = _git(repository, "commit", "-m", "root")
    assert committed.returncode == 0, committed.stderr


class ConventionsTests(unittest.TestCase):
    def test_conventions_section_carries_the_four_always_on_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            result = _run_init(repository)
            self.assertEqual(result.returncode, 0, result.stderr)

            instructions = (repository / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("WORK.md", instructions)
            self.assertIn("Work-Item: <id>", instructions)
            self.assertIn(".agentflow/proposals/", instructions)
            self.assertIn("Never edit `.agentflow/work/` directly", instructions)


class GitHookTests(unittest.TestCase):
    def _init(self, repository: Path, *extra: str) -> dict:
        result = _run_init(repository, *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def _stage_and_commit(
        self, repository: Path, message: str
    ) -> subprocess.CompletedProcess:
        _git(repository, "add", "-A")
        return _git(repository, "commit", "-m", message)

    def test_pre_commit_refuses_work_graph_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_git_repo(repository)
            self._init(repository)

            work_dir = repository / ".agentflow" / "work"
            work_dir.mkdir(parents=True, exist_ok=True)
            (work_dir / "roadmap.jsonl").write_text("{}\n", encoding="utf-8")
            _git(repository, "add", ".agentflow/work/roadmap.jsonl")
            committed = _git(repository, "commit", "-m", "tamper with graph")

            self.assertNotEqual(committed.returncode, 0)
            self.assertIn(".agentflow/work", committed.stderr)

    def test_pre_commit_allows_proposals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_git_repo(repository)
            self._init(repository)

            proposals = repository / ".agentflow" / "proposals"
            proposals.mkdir(parents=True, exist_ok=True)
            (proposals / "idea.json").write_text(
                '{"kind": "new-work"}\n', encoding="utf-8"
            )
            committed = self._stage_and_commit(repository, "drop a proposal")

            self.assertEqual(committed.returncode, 0, committed.stderr)

    def test_observe_mode_never_blocks_ordinary_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_git_repo(repository)
            response = self._init(repository)
            self.assertEqual(response["enforcement"], "observe")

            # A developer with no Agentflow knowledge, no trailer: not blocked.
            (repository / "feature.py").write_text("x = 1\n", encoding="utf-8")
            committed = self._stage_and_commit(repository, "add a feature")

            self.assertEqual(committed.returncode, 0, committed.stderr)
            self.assertIn("observe", committed.stderr)

    def test_strict_mode_requires_a_work_item_trailer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_git_repo(repository)
            response = self._init(repository, "--enforcement", "strict")
            self.assertEqual(response["enforcement"], "strict")

            (repository / "feature.py").write_text("x = 1\n", encoding="utf-8")
            _git(repository, "add", "-A")
            without = _git(repository, "commit", "-m", "no trailer")
            self.assertNotEqual(without.returncode, 0)

            with_trailer = _git(
                repository,
                "commit",
                "-m",
                "add a feature\n\nWork-Item: some-item",
            )
            self.assertEqual(with_trailer.returncode, 0, with_trailer.stderr)

    def test_hooks_are_never_written_outside_the_target_repository(self) -> None:
        """A shared ``core.hooksPath`` must not turn init into a cross-repo write.

        Many developers point ``core.hooksPath`` at a directory shared across
        several repositories (a common setup for org-wide hook management).
        ``agentflow init`` only has authorization to configure the repository
        it was asked to initialize; installing hook files into a location that
        lives outside that repository would silently rewrite hooks other,
        unrelated repositories rely on. Confinement to the target repository is
        required regardless of git hook configuration.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            shared_hooks = base / "shared-hooks-outside-repo"
            shared_hooks.mkdir(parents=True, exist_ok=True)
            repository = base / "repo"
            _init_git_repo(repository)
            _git(
                repository,
                "config",
                "core.hooksPath",
                str(shared_hooks),
            )

            result = _run_init(repository)
            self.assertEqual(result.returncode, 0, result.stderr)

            for name in ("pre-commit", "commit-msg"):
                written = shared_hooks / name
                self.assertFalse(
                    written.exists(),
                    f"agentflow init wrote '{written}', which lies outside the "
                    "target repository (it is only authorized to configure "
                    f"'{repository}'). A shared core.hooksPath must not let "
                    "init rewrite hooks belonging to other repositories.",
                )

    def test_foreign_hook_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_git_repo(repository)
            hooks_dir = repository / ".git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            foreign = hooks_dir / "pre-commit"
            foreign.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")

            response = self._init(repository)

            self.assertIn("pre-commit", response["hooks_preserved"])
            self.assertIn("commit-msg", response["hooks_installed"])
            self.assertEqual(
                foreign.read_text(encoding="utf-8"), "#!/bin/sh\necho mine\n"
            )


class PolicyCommittabilityTests(unittest.TestCase):
    def test_policy_defaults_to_observe_and_is_committable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_git_repo(repository)
            result = _run_init(repository)
            self.assertEqual(result.returncode, 0, result.stderr)

            policy = json.loads(
                (repository / ".agentflow" / "policy.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(policy["enforcement"], "observe")

            checked = _git(
                repository, "check-ignore", ".agentflow/policy.json"
            )
            self.assertEqual(checked.returncode, 1)  # not ignored

    def test_reinit_preserves_a_chosen_strict_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_git_repo(repository)
            _run_init(repository, "--enforcement", "strict")
            result = _run_init(repository)  # no flag
            self.assertEqual(result.returncode, 0, result.stderr)
            policy = json.loads(
                (repository / ".agentflow" / "policy.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(policy["enforcement"], "strict")

    def _assert_uncommittable(self, repository: Path, rule_fragment: str) -> None:
        result = _run_init(repository)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(".agentflow/policy.json", result.stderr)
        self.assertIn(rule_fragment, result.stderr)
        # It names a fix rather than defeating the ignore configuration.
        self.assertIn("re-run", result.stderr)
        self.assertFalse((repository / ".agentflow" / "policy.json").exists())

    def test_directory_ignore_form_fails_actionably(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_git_repo(repository)
            (repository / ".gitignore").write_text(".agentflow/\n", encoding="utf-8")
            self._assert_uncommittable(repository, ".agentflow/")

    def test_glob_ignore_form_fails_actionably(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_git_repo(repository)
            (repository / ".gitignore").write_text("*.json\n", encoding="utf-8")
            self._assert_uncommittable(repository, "*.json")

    def test_nested_ignore_form_fails_actionably(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_git_repo(repository)
            agentflow_dir = repository / ".agentflow"
            agentflow_dir.mkdir(parents=True, exist_ok=True)
            (agentflow_dir / ".gitignore").write_text("policy.json\n", encoding="utf-8")
            self._assert_uncommittable(repository, "policy.json")

    def _apply_suggested_fix(self, repository: Path, stderr: str) -> None:
        """Append the exact negation lines the tool suggested to the file it named.

        A file inside a directory-ignored tree cannot be re-included by negating
        the file alone, so the suggestion must name each ancestor directory too.
        The test applies whatever the tool actually printed — a dead-end
        suggestion would fail to resolve committability here.
        """
        match = re.search(r"append these lines to '([^']+)':\n", stderr)
        self.assertIsNotNone(match, f"no actionable fix in message:\n{stderr}")
        fix_file = repository / match.group(1)
        remainder = stderr[match.end() :]
        lines = []
        for raw in remainder.splitlines():
            if raw.startswith("    "):
                lines.append(raw[4:])
            else:
                break
        self.assertTrue(lines, f"no suggested lines in message:\n{stderr}")
        existing = fix_file.read_text(encoding="utf-8") if fix_file.exists() else ""
        fix_file.parent.mkdir(parents=True, exist_ok=True)
        fix_file.write_text(existing + "".join(f"{line}\n" for line in lines))

    def test_directory_ignore_form_suggested_fix_actually_works(self) -> None:
        # A directory-form ignore rule (e.g. '.agentflow/') excludes the
        # directory itself, so git cannot re-include a file inside it by
        # negating the file alone. The tool's suggested fix must therefore
        # re-include the ancestor directory too; applying exactly what it
        # printed must resolve committability rather than reproduce the error.
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_git_repo(repository)
            (repository / ".gitignore").write_text(".agentflow/\n", encoding="utf-8")

            first = _run_init(repository)
            self.assertEqual(first.returncode, 1, first.stdout)

            self._apply_suggested_fix(repository, first.stderr)
            second = _run_init(repository)
            self.assertEqual(
                second.returncode,
                0,
                "applying the tool's own suggested fix verbatim did not "
                f"resolve committability: {second.stderr}",
            )

    def test_negated_reinclude_is_treated_as_committable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_git_repo(repository)
            # Note: a negation cannot re-include a file whose parent directory
            # is itself excluded, so ignore the directory's contents (not the
            # directory) and re-include the policy file.
            (repository / ".gitignore").write_text(
                ".agentflow/*\n!.agentflow/policy.json\n", encoding="utf-8"
            )
            result = _run_init(repository)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((repository / ".agentflow" / "policy.json").exists())


if __name__ == "__main__":
    unittest.main()
