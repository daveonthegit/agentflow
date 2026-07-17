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

from agentflow.drift import (  # noqa: E402
    KIND_CLOSED_ITEM,
    KIND_SCOPE_DRIFT,
    KIND_UNKNOWN_ITEM,
    KIND_UNTRACKED,
    CommitRecord,
    classify_drift,
    detect_work_drift,
)


def _item(item_id: str, **extra: object) -> dict:
    item = {
        "id": item_id,
        "summary": f"summary for {item_id}",
        "acceptance_criteria": [f"{item_id} works"],
        "depends_on": [],
    }
    item.update(extra)
    return item


def run_agentflow(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agentflow", *args],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )


def run_drift(repository: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    # No --data-dir: the command must need no Agentflow Home state.
    return run_agentflow(
        "work", "drift", "--repository", str(repository), *extra, cwd=repository
    )


class ClassifyDriftTests(unittest.TestCase):
    """The pure classifier is deterministic and covers every finding kind."""

    def test_untracked_commit_has_no_trailer(self) -> None:
        commit = CommitRecord(sha="a1", subject="manual fix")
        findings = classify_drift([commit], [_item("known")], {"known"})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, KIND_UNTRACKED)
        self.assertIsNone(findings[0].work_item_id)

    def test_trailer_for_open_item_is_clean(self) -> None:
        commit = CommitRecord(sha="a1", subject="do work", trailers=("known",))
        findings = classify_drift([commit], [_item("known")], {"known"})
        self.assertEqual(findings, ())

    def test_unknown_trailer_never_existed(self) -> None:
        commit = CommitRecord(sha="a1", subject="typo", trailers=("typo-id",))
        findings = classify_drift([commit], [_item("known")], {"known"})
        self.assertEqual([f.kind for f in findings], [KIND_UNKNOWN_ITEM])
        self.assertEqual(findings[0].work_item_id, "typo-id")

    def test_closed_trailer_existed_in_history(self) -> None:
        commit = CommitRecord(sha="a1", subject="late work", trailers=("done",))
        findings = classify_drift([commit], [_item("known")], {"known", "done"})
        self.assertEqual([f.kind for f in findings], [KIND_CLOSED_ITEM])
        self.assertEqual(findings[0].work_item_id, "done")

    def test_scope_drift_touches_open_item_without_trailer(self) -> None:
        commit = CommitRecord(
            sha="a1",
            subject="edit scoped file",
            trailers=("other",),
            changed_paths=("src/pkg/mod.py",),
        )
        graph = [
            _item("scoped", files=["src/pkg/**"]),
            _item("other"),
        ]
        findings = classify_drift([commit], graph, {"scoped", "other"})
        self.assertIn(
            (KIND_SCOPE_DRIFT, "scoped"),
            [(f.kind, f.work_item_id) for f in findings],
        )

    def test_scope_carrying_the_item_trailer_is_clean(self) -> None:
        commit = CommitRecord(
            sha="a1",
            subject="edit scoped file",
            trailers=("scoped",),
            changed_paths=("src/pkg/mod.py",),
        )
        graph = [_item("scoped", files=["src/pkg/**"])]
        findings = classify_drift([commit], graph, {"scoped"})
        self.assertEqual(findings, ())

    def test_untracked_commit_in_scope_is_reported_twice(self) -> None:
        # An untracked commit that also edits an open item's scope is both
        # untracked and scope_drift: two independent, actionable statements.
        commit = CommitRecord(
            sha="a1",
            subject="unattributed scoped edit",
            changed_paths=("src/pkg/mod.py",),
        )
        graph = [_item("scoped", files=["src/pkg/**"])]
        findings = classify_drift([commit], graph, {"scoped"})
        self.assertEqual(
            [(f.kind, f.work_item_id) for f in findings],
            [(KIND_UNTRACKED, None), (KIND_SCOPE_DRIFT, "scoped")],
        )

    def test_ordering_is_deterministic(self) -> None:
        graph = [
            _item("alpha", files=["src/a/**"]),
            _item("beta", files=["src/b/**"]),
        ]
        commit = CommitRecord(
            sha="a1",
            subject="wide edit",
            trailers=("zeta", "mu"),
            changed_paths=("src/a/x.py", "src/b/y.py"),
        )
        findings = classify_drift([commit], graph, {"alpha", "beta"})
        # Trailer findings sorted by id, then scope-drift in graph order.
        self.assertEqual(
            [(f.kind, f.work_item_id) for f in findings],
            [
                (KIND_UNKNOWN_ITEM, "mu"),
                (KIND_UNKNOWN_ITEM, "zeta"),
                (KIND_SCOPE_DRIFT, "alpha"),
                (KIND_SCOPE_DRIFT, "beta"),
            ],
        )


class _Repo:
    """A throwaway git repository fixture for drift integration tests."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._run("git", "init")
        self._run("git", "config", "user.email", "agentflow@example.test")
        self._run("git", "config", "user.name", "Agentflow Test")

    def _run(self, *args: str) -> None:
        subprocess.run(list(args), cwd=self.path, check=True, capture_output=True)

    def write_graph(self, items: list[dict]) -> None:
        work_dir = self.path / ".agentflow" / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "graph.jsonl").write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in items),
            encoding="utf-8",
        )

    def write(self, relative: str, content: str) -> None:
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

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

    def checkout_new_branch(self, name: str) -> None:
        self._run("git", "checkout", "-q", "-b", name)

    def checkout(self, name: str) -> None:
        self._run("git", "checkout", "-q", name)

    def current_branch(self) -> str:
        return subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=self.path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def merge_no_ff(self, branch: str, message: str) -> str:
        # Mirrors the merger's own "merge" strategy (git merge --no-ff),
        # which is exactly what this repository's own merge_policy uses.
        self._run("git", "merge", "--no-ff", branch, "-m", message)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def record_approval(self) -> str:
        # A minimal approval mirror line; drift keys off the commit that lands
        # it, never its content, so an exact hash is unnecessary here.
        self.write(
            ".agentflow/approvals.jsonl",
            json.dumps(
                {
                    "approved_at": "2026-07-17T00:00:00+00:00",
                    "approved_by": "dave",
                    "graph_hash": "0" * 64,
                    "repository": str(self.path),
                    "sequence": 1,
                    "type": "work_graph_approved",
                }
            )
            + "\n",
        )
        return self.commit("Approve work graph")


class DetectWorkDriftTests(unittest.TestCase):
    def _repo(self) -> _Repo:
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", tmp], check=False))
        return _Repo(Path(tmp))

    def test_boundary_is_the_latest_approval_commit(self) -> None:
        repo = self._repo()
        repo.write_graph([_item("a")])
        repo.write("README.md", "# repo\n")
        repo.commit("init")
        boundary = repo.record_approval()
        repo.write("after.txt", "later\n")
        repo.commit("later manual work")

        report = detect_work_drift(repo.path)
        self.assertEqual(report.approval_boundary, boundary)
        # Only the post-approval commit is analyzed.
        self.assertEqual(len(report.analyzed_commits), 1)
        self.assertEqual([f.kind for f in report.findings], [KIND_UNTRACKED])

    def test_untracked_unknown_closed_and_scope(self) -> None:
        repo = self._repo()
        repo.write_graph(
            [
                _item("open-item", files=["src/scoped/**"]),
                _item("shipped"),
            ]
        )
        repo.write("README.md", "# repo\n")
        repo.commit("init graph")
        # Close "shipped" by editing it out; its id stays in git history.
        repo.write_graph([_item("open-item", files=["src/scoped/**"])])
        repo.commit("ship shipped")
        repo.record_approval()

        repo.write("notes.txt", "manual\n")
        repo.commit("untracked manual change")

        repo.write("x.txt", "x\n")
        repo.commit(
            "reference a bogus item\n\nWork-Item: does-not-exist"
        )

        repo.write("y.txt", "y\n")
        repo.commit("more work on a done item\n\nWork-Item: shipped")

        repo.write("src/scoped/module.py", "print('hi')\n")
        repo.commit("edit scoped file\n\nWork-Item: does-not-exist")

        report = detect_work_drift(repo.path)
        kinds = {(f.kind, f.work_item_id) for f in report.findings}
        self.assertIn((KIND_UNTRACKED, None), kinds)
        self.assertIn((KIND_UNKNOWN_ITEM, "does-not-exist"), kinds)
        self.assertIn((KIND_CLOSED_ITEM, "shipped"), kinds)
        self.assertIn((KIND_SCOPE_DRIFT, "open-item"), kinds)

    def test_clean_when_trailer_matches_open_item(self) -> None:
        repo = self._repo()
        repo.write_graph([_item("open-item", files=["src/scoped/**"])])
        repo.write("README.md", "# repo\n")
        repo.commit("init")
        repo.record_approval()
        repo.write("src/scoped/module.py", "print('hi')\n")
        repo.commit("scoped work\n\nWork-Item: open-item")

        report = detect_work_drift(repo.path)
        self.assertFalse(report.has_findings)

    def test_no_approval_walks_full_history(self) -> None:
        repo = self._repo()
        repo.write_graph([_item("a")])
        repo.write("README.md", "# repo\n")
        repo.commit("init without approval")
        report = detect_work_drift(repo.path)
        self.assertIsNone(report.approval_boundary)
        self.assertEqual(len(report.analyzed_commits), 1)

    def test_no_ff_merge_folding_in_attributed_work_is_not_untracked(self) -> None:
        # This repository's own merge_policy strategy is "merge", which the
        # merger implements as `git merge --no-ff -m "Agentflow run <id>
        # merge" <sha>` (see merger.py) — a commit that never carries a
        # Work-Item trailer and, via plain `git show`, never reports changed
        # paths either (a git quirk: `git show --name-only` on a merge
        # commit is empty unless -m/-c is passed). Every commit actually
        # landed on the topic branch already carries its own Work-Item
        # trailer. The merge commit itself is just plumbing for a gated,
        # fully-attributed run and must not be reported as drift.
        repo = self._repo()
        repo.write_graph([_item("a")])
        repo.write("README.md", "# repo\n")
        repo.commit("init")
        repo.record_approval()
        base_branch = repo.current_branch()

        repo.checkout_new_branch("feature")
        repo.write("work.py", "x = 1\n")
        repo.commit("do the work\n\nWork-Item: a")
        repo.checkout(base_branch)
        repo.merge_no_ff("feature", "Agentflow run xyz merge")

        report = detect_work_drift(repo.path)
        self.assertEqual(
            report.findings,
            (),
            f"merge commit produced spurious drift findings: {report.findings}",
        )


class DriftCliTests(unittest.TestCase):
    def _repo(self) -> _Repo:
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", tmp], check=False))
        return _Repo(Path(tmp))

    def test_observe_mode_reports_findings_but_exits_zero(self) -> None:
        repo = self._repo()
        repo.write_graph([_item("a")])
        repo.write("README.md", "# repo\n")
        repo.commit("init")
        repo.record_approval()
        repo.write("manual.txt", "manual\n")
        repo.commit("manual work with no trailer")

        result = run_drift(repo.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["state"], "drift")
        self.assertEqual(
            [f["kind"] for f in report["findings"]], [KIND_UNTRACKED]
        )

    def test_strict_mode_exits_nonzero_on_findings(self) -> None:
        repo = self._repo()
        repo.write_graph([_item("a")])
        repo.write("README.md", "# repo\n")
        repo.commit("init")
        repo.record_approval()
        repo.write("manual.txt", "manual\n")
        repo.commit("manual work with no trailer")

        result = run_drift(repo.path, "--strict")
        self.assertEqual(result.returncode, 1, result.stdout)

    def test_strict_mode_exits_zero_when_clean(self) -> None:
        repo = self._repo()
        repo.write_graph([_item("a", files=["src/**"])])
        repo.write("README.md", "# repo\n")
        repo.commit("init")
        repo.record_approval()
        repo.write("src/mod.py", "x = 1\n")
        repo.commit("scoped work\n\nWork-Item: a")

        result = run_drift(repo.path, "--strict")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "clean")

    def test_command_mutates_nothing(self) -> None:
        repo = self._repo()
        repo.write_graph([_item("a")])
        repo.write("README.md", "# repo\n")
        repo.commit("init")
        repo.record_approval()
        repo.write("manual.txt", "manual\n")
        repo.commit("manual work")

        before = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo.path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        run_drift(repo.path)
        after = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo.path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(before, after)
        self.assertEqual(after, "")


if __name__ == "__main__":
    unittest.main()
