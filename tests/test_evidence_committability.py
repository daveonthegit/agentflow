"""Every repo-tracked evidence writer proves its path is committable.

``agentflow init`` refuses to write ``.agentflow/policy.json`` when git's ignore
rules would swallow it (see ``test_init_conventions_and_hooks``). The same
guarantee must hold for every *other* file Agentflow relies on being shared
through the repository: the Work Graph approval mirror, external-completion
evidence, and the proposals inbox. A repository whose ignore rules match one of
those paths would otherwise record evidence locally that no teammate or CI
runner ever receives -- silently unshared, with no error at write time.

These tests drive each writer against a repository that ignores its evidence
path and assert one actionable message naming the offending rule, and that the
writer changes nothing before it fails.
"""

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
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentflow.committability import EvidenceNotCommittableError  # noqa: E402
from agentflow.contracts import DISPOSITION_COMPLETED_EXTERNALLY  # noqa: E402
from agentflow.proposals import ingest_proposals  # noqa: E402
from agentflow.work_graph import (  # noqa: E402
    approve_work_graph,
    load_work_graph,
    read_work_graph_approvals,
)
from agentflow.work_reconcile import (  # noqa: E402
    EXTERNAL_COMPLETIONS_RELATIVE,
    apply_reconcile,
)


ITEM = {
    "id": "open-item",
    "summary": "Do the work",
    "acceptance_criteria": ["it is done"],
    "depends_on": [],
}


def _agentflow(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agentflow", *args],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )


class _Repo:
    """A throwaway git repository fixture with a Work Graph."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._run("git", "init")
        self._run("git", "config", "user.email", "agentflow@example.test")
        self._run("git", "config", "user.name", "Agentflow Test")

    def _run(self, *args: str) -> None:
        subprocess.run(list(args), cwd=self.path, check=True, capture_output=True)

    def write(self, relative: str, content: str) -> None:
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def write_graph(self, items: list[dict]) -> None:
        self.write(
            ".agentflow/work/graph.jsonl",
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in items),
        )

    def ignore(self, *rules: str) -> None:
        self.write(".gitignore", "".join(f"{rule}\n" for rule in rules))

    def commit(self, message: str) -> str:
        self._run("git", "add", "-A")
        self._run("git", "commit", "-m", message)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()


class _RepoCase(unittest.TestCase):
    def _repo(self) -> _Repo:
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", tmp], check=False))
        return _Repo(Path(tmp))

    def assertNamesRule(
        self, error: EvidenceNotCommittableError, evidence: str, rule_fragment: str
    ) -> None:
        message = str(error)
        self.assertIn(evidence, message)
        self.assertIn(rule_fragment, message)
        # Actionable: it names a fix and a retry rather than a bare failure.
        self.assertIn("append these lines to", message)
        self.assertIn("re-run", message)


class ApproveCommittabilityTests(_RepoCase):
    def test_ignored_approval_mirror_fails_before_any_write(self) -> None:
        repo = self._repo()
        repo.write_graph([ITEM])
        repo.ignore(".agentflow/approvals.jsonl")
        repo.commit("init")
        home = repo.path / "home"

        with self.assertRaises(EvidenceNotCommittableError) as caught:
            approve_work_graph(
                repository=repo.path,
                data_dir=home,
                approved_by="dave",
            )

        self.assertNamesRule(
            caught.exception, ".agentflow/approvals.jsonl", "approvals.jsonl"
        )
        # Nothing was recorded: not the repo mirror, and not the home evidence
        # log the mirror is supposed to corroborate.
        self.assertFalse((repo.path / ".agentflow" / "approvals.jsonl").exists())
        self.assertEqual(read_work_graph_approvals(home), [])

    def test_directory_ignore_suggestion_reincludes_ancestors(self) -> None:
        # A directory-form rule ('.agentflow/') excludes the directory itself, so
        # the fix must re-include each ancestor, not just the file -- exactly as
        # init does for policy.json. Applying the tool's own words must resolve
        # committability.
        repo = self._repo()
        repo.write_graph([ITEM])
        repo.ignore(".agentflow/")
        # (No commit needed: check-ignore reads the working tree's rules.)

        with self.assertRaises(EvidenceNotCommittableError) as caught:
            approve_work_graph(
                repository=repo.path, data_dir=repo.path / "home", approved_by="dave"
            )

        message = str(caught.exception)
        self.assertIn(".agentflow/", message)
        fix_file, lines = _parse_fix(message)
        self.assertIn("!.agentflow/", lines)
        self.assertIn("!.agentflow/approvals.jsonl", lines)
        existing = (repo.path / fix_file).read_text(encoding="utf-8")
        (repo.path / fix_file).write_text(
            existing + "".join(f"{line}\n" for line in lines), encoding="utf-8"
        )
        # The path is committable now; approval succeeds and writes the mirror.
        approve_work_graph(
            repository=repo.path, data_dir=repo.path / "home", approved_by="dave"
        )
        self.assertTrue((repo.path / ".agentflow" / "approvals.jsonl").is_file())

    def test_committable_mirror_approves_normally(self) -> None:
        repo = self._repo()
        repo.write_graph([ITEM])
        repo.commit("init")
        approve_work_graph(
            repository=repo.path, data_dir=repo.path / "home", approved_by="dave"
        )
        self.assertTrue((repo.path / ".agentflow" / "approvals.jsonl").is_file())


class ReconcileCommittabilityTests(_RepoCase):
    def _repo_with_item(self) -> _Repo:
        repo = self._repo()
        repo.write_graph([ITEM])
        repo.write("README.md", "# repo\n")
        return repo

    def test_ignored_external_log_fails_before_mutating_the_graph(self) -> None:
        repo = self._repo_with_item()
        repo.ignore(".agentflow/external-completions.jsonl")
        repo.commit("init")

        with self.assertRaises(EvidenceNotCommittableError) as caught:
            apply_reconcile(
                repo.path,
                [
                    {
                        "work_item_id": "open-item",
                        "disposition": DISPOSITION_COMPLETED_EXTERNALLY,
                        "confirmed": True,
                        "external_commits": ["abc123"],
                    }
                ],
                confirmed_by="dave",
            )

        self.assertNamesRule(
            caught.exception,
            ".agentflow/external-completions.jsonl",
            "external-completions.jsonl",
        )
        # The graph is untouched -- the item was not removed -- and no evidence
        # log was written.
        self.assertEqual(
            [item["id"] for item in load_work_graph(repo.path)], ["open-item"]
        )
        self.assertFalse((repo.path / EXTERNAL_COMPLETIONS_RELATIVE).exists())

    def test_a_still_valid_pass_needs_no_external_log(self) -> None:
        # No completed_externally disposition means no external-completion
        # evidence, so ignoring that path must not block an unrelated pass.
        repo = self._repo_with_item()
        repo.ignore(".agentflow/external-completions.jsonl")
        repo.commit("init")

        result = apply_reconcile(
            repo.path,
            [
                {
                    "work_item_id": "open-item",
                    "disposition": "still_valid",
                    "confirmed": True,
                    "external_commits": [],
                }
            ],
            confirmed_by="dave",
        )
        self.assertEqual(
            result.applied,
            [{"work_item_id": "open-item", "disposition": "still_valid"}],
        )


class IngestCommittabilityTests(_RepoCase):
    def _repo_with_proposal(self) -> _Repo:
        repo = self._repo()
        repo.write_graph([ITEM])
        repo.write(
            ".agentflow/proposals/new.json",
            json.dumps(
                {
                    "kind": "new-work",
                    "summary": "A fresh idea",
                    "acceptance_criteria": ["it works"],
                }
            ),
        )
        return repo

    def test_ignored_inbox_fails_without_consuming_proposals(self) -> None:
        repo = self._repo_with_proposal()
        repo.ignore(".agentflow/proposals/")
        repo.commit("init")

        with self.assertRaises(EvidenceNotCommittableError) as caught:
            ingest_proposals(repo.path)

        self.assertNamesRule(
            caught.exception, ".agentflow/proposals", "proposals/"
        )
        # The proposal file is left in place; nothing was ingested.
        self.assertTrue(
            (repo.path / ".agentflow" / "proposals" / "new.json").is_file()
        )
        self.assertEqual([item["id"] for item in load_work_graph(repo.path)], ["open-item"])

    def test_committable_inbox_ingests_normally(self) -> None:
        repo = self._repo_with_proposal()
        repo.commit("init")
        result = ingest_proposals(repo.path)
        self.assertEqual(result.removed, ["new.json"])

    def test_ignored_inbox_fails_even_before_any_proposal_is_dropped(self) -> None:
        # The proposals directory does not exist on disk until some agent
        # drops a file into it. A directory-only ignore pattern like
        # "proposals/" is only detected by ``git check-ignore`` once the
        # directory actually exists -- so the very first ``work ingest`` on a
        # fresh clone (or any ingest run between proposal drops) must still
        # catch an ignored inbox before a proposal ever silently vanishes
        # into it.
        repo = self._repo()
        repo.write_graph([ITEM])
        repo.ignore(".agentflow/proposals/")
        repo.commit("init")
        self.assertFalse((repo.path / ".agentflow" / "proposals").exists())

        with self.assertRaises(EvidenceNotCommittableError) as caught:
            ingest_proposals(repo.path)

        self.assertNamesRule(caught.exception, ".agentflow/proposals", "proposals/")


class CliCommittabilityTests(_RepoCase):
    def test_ingest_cli_exits_actionably_not_with_a_traceback(self) -> None:
        repo = self._repo()
        repo.write_graph([ITEM])
        repo.write(
            ".agentflow/proposals/new.json",
            json.dumps(
                {
                    "kind": "new-work",
                    "summary": "A fresh idea",
                    "acceptance_criteria": ["it works"],
                }
            ),
        )
        repo.ignore(".agentflow/proposals/")
        repo.commit("init")

        result = _agentflow(
            "work", "ingest", "--repository", str(repo.path), cwd=repo.path
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(".agentflow/proposals", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


def _parse_fix(message: str) -> tuple[str, list[str]]:
    match = re.search(r"append these lines to '([^']+)':\n", message)
    assert match is not None, message
    fix_file = match.group(1)
    lines: list[str] = []
    for raw in message[match.end() :].splitlines():
        if raw.startswith("    "):
            lines.append(raw[4:])
        else:
            break
    return fix_file, lines


if __name__ == "__main__":
    unittest.main()
