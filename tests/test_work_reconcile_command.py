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

from agentflow.contracts import (  # noqa: E402
    ContractError,
    DISPOSITION_COMPLETED_EXTERNALLY,
    DISPOSITION_INVALIDATED,
    DISPOSITION_PARTIALLY_DONE,
    DISPOSITION_STILL_VALID,
    WORK_ITEM_STATUS_PROPOSED,
    validate_reconcile_disposition,
)
from agentflow.work_graph import load_work_graph  # noqa: E402
from agentflow.work_reconcile import (  # noqa: E402
    EXTERNAL_COMPLETION_TYPE,
    EXTERNAL_COMPLETIONS_RELATIVE,
    apply_reconcile,
    plan_reconcile,
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


def _disposition(work_item_id: str, disposition: str, **extra: object) -> dict:
    record = {
        "work_item_id": work_item_id,
        "disposition": disposition,
        "confirmed": True,
        "external_commits": [],
        "amended_acceptance_criteria": [],
    }
    record.update(extra)
    return record


class ValidateDispositionTests(unittest.TestCase):
    def test_still_valid_round_trips(self) -> None:
        record = validate_reconcile_disposition(
            _disposition("a", DISPOSITION_STILL_VALID, confirmed=False)
        )
        self.assertEqual(record["disposition"], DISPOSITION_STILL_VALID)
        self.assertFalse(record["confirmed"])

    def test_completed_externally_requires_a_commit(self) -> None:
        with self.assertRaises(ContractError):
            validate_reconcile_disposition(
                _disposition("a", DISPOSITION_COMPLETED_EXTERNALLY)
            )

    def test_external_commits_only_on_completed_externally(self) -> None:
        with self.assertRaises(ContractError):
            validate_reconcile_disposition(
                _disposition("a", DISPOSITION_STILL_VALID, external_commits=["abc"])
            )

    def test_partially_done_requires_amended_criteria(self) -> None:
        with self.assertRaises(ContractError):
            validate_reconcile_disposition(
                _disposition("a", DISPOSITION_PARTIALLY_DONE)
            )

    def test_amended_criteria_only_on_partially_done(self) -> None:
        with self.assertRaises(ContractError):
            validate_reconcile_disposition(
                _disposition(
                    "a", DISPOSITION_INVALIDATED, amended_acceptance_criteria=["x"]
                )
            )

    def test_unknown_disposition_and_fields_rejected(self) -> None:
        with self.assertRaises(ContractError):
            validate_reconcile_disposition(_disposition("a", "invented"))
        with self.assertRaises(ContractError):
            validate_reconcile_disposition(
                {**_disposition("a", DISPOSITION_STILL_VALID), "extra": 1}
            )

    def test_confirmed_must_be_boolean(self) -> None:
        with self.assertRaises(ContractError):
            validate_reconcile_disposition(
                _disposition("a", DISPOSITION_STILL_VALID, confirmed="yes")
            )


class _Repo:
    """A throwaway git repository fixture with a Work Graph and proposals."""

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

    def write_claim(self, filename: str, summary: str, relates_to: list[str]) -> None:
        self.write(
            f".agentflow/proposals/{filename}",
            json.dumps(
                {
                    "kind": "completion-claim",
                    "summary": summary,
                    "relates_to": relates_to,
                }
            ),
        )

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

    def record_approval(self) -> str:
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


class _RepoCase(unittest.TestCase):
    def _repo(self) -> _Repo:
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", tmp], check=False))
        return _Repo(Path(tmp))

    def _drifted(self, *, files: str = "src/scoped/**") -> tuple[_Repo, str]:
        """A repo where a commit drifted into ``open-item``'s scope after approval."""
        repo = self._repo()
        repo.write_graph([_item("open-item", files=[files])])
        repo.write("README.md", "# repo\n")
        repo.commit("init")
        repo.record_approval()
        repo.write("src/scoped/mod.py", "print('external work')\n")
        drift_commit = repo.commit("land scoped work with no trailer")
        return repo, drift_commit


class PlanReconcileTests(_RepoCase):
    def test_claim_plus_drift_proposes_completed_externally(self) -> None:
        repo, drift_commit = self._drifted()
        repo.write_claim("done.json", "open-item is finished", ["open-item"])

        plan = plan_reconcile(repo.path)

        self.assertEqual(len(plan.dispositions), 1)
        disposition = plan.dispositions[0]
        self.assertEqual(disposition["work_item_id"], "open-item")
        self.assertEqual(
            disposition["disposition"], DISPOSITION_COMPLETED_EXTERNALLY
        )
        self.assertEqual(disposition["external_commits"], [drift_commit])
        # Nothing is applied by planning: it stays a proposal.
        self.assertFalse(disposition["confirmed"])

    def test_drift_without_a_claim_defaults_to_still_valid(self) -> None:
        repo, _ = self._drifted()

        plan = plan_reconcile(repo.path)

        self.assertEqual(len(plan.dispositions), 1)
        self.assertEqual(
            plan.dispositions[0]["disposition"], DISPOSITION_STILL_VALID
        )
        self.assertEqual(plan.dispositions[0]["external_commits"], [])

    def test_claim_without_attributable_commits_is_still_valid(self) -> None:
        # A claim naming an item nothing drifted into cannot honestly propose
        # completed_externally: there is no commit to name.
        repo = self._repo()
        repo.write_graph([_item("open-item", files=["src/scoped/**"])])
        repo.write("README.md", "# repo\n")
        repo.commit("init")
        repo.record_approval()
        repo.write_claim("done.json", "open-item is finished", ["open-item"])

        plan = plan_reconcile(repo.path)
        self.assertEqual(
            plan.dispositions[0]["disposition"], DISPOSITION_STILL_VALID
        )

    def test_proposed_items_are_ineligible_in_planning(self) -> None:
        repo, _ = self._drifted()
        # Re-declare the drifted item as proposed and re-approve boundary layout.
        repo.write_graph(
            [_item("open-item", files=["src/scoped/**"], status=WORK_ITEM_STATUS_PROPOSED)]
        )
        repo.commit("mark item proposed")
        repo.write_claim("done.json", "open-item is finished", ["open-item"])

        plan = plan_reconcile(repo.path)
        self.assertEqual(plan.dispositions, [])
        self.assertEqual(
            plan.ineligible, [{"work_item_id": "open-item", "reason": "proposed"}]
        )


class ApplyReconcileTests(_RepoCase):
    def test_completed_externally_removes_item_and_records_evidence(self) -> None:
        repo, drift_commit = self._drifted()
        repo.write_claim("done.json", "open-item is finished", ["open-item"])

        result = apply_reconcile(
            repo.path,
            [
                _disposition(
                    "open-item",
                    DISPOSITION_COMPLETED_EXTERNALLY,
                    external_commits=[drift_commit],
                )
            ],
            confirmed_by="dave",
        )

        # The item leaves the graph; completion is derived from being edited out.
        self.assertEqual(load_work_graph(repo.path), [])
        self.assertEqual(
            result.applied,
            [{"work_item_id": "open-item", "disposition": DISPOSITION_COMPLETED_EXTERNALLY}],
        )
        # Attributed external evidence, explicitly not a Run.
        log = repo.path / EXTERNAL_COMPLETIONS_RELATIVE
        self.assertTrue(log.is_file())
        record = json.loads(log.read_text().splitlines()[0])
        self.assertEqual(record["type"], EXTERNAL_COMPLETION_TYPE)
        self.assertEqual(record["work_item_id"], "open-item")
        self.assertEqual(record["external_commits"], [drift_commit])
        self.assertEqual(record["confirmed_by"], "dave")
        # No Run Evidence is fabricated anywhere in the repository.
        self.assertFalse((repo.path / ".agentflow" / "runs").exists())
        # The consumed claim is removed.
        self.assertEqual(result.removed_claims, ["done.json"])
        self.assertFalse((repo.path / ".agentflow" / "proposals" / "done.json").exists())

    def test_completed_externally_strips_dependents(self) -> None:
        repo = self._repo()
        repo.write_graph(
            [
                _item("base", files=["src/scoped/**"]),
                _item("dependent", depends_on=["base"]),
            ]
        )
        repo.commit("init")

        apply_reconcile(
            repo.path,
            [
                _disposition(
                    "base", DISPOSITION_COMPLETED_EXTERNALLY, external_commits=["abc123"]
                )
            ],
            confirmed_by="dave",
        )
        graph = load_work_graph(repo.path)
        self.assertEqual([item["id"] for item in graph], ["dependent"])
        # The dependency on the removed item is stripped so the graph stays valid.
        self.assertEqual(graph[0]["depends_on"], [])

    def test_partially_done_amends_acceptance_criteria(self) -> None:
        repo, _ = self._drifted()
        result = apply_reconcile(
            repo.path,
            [
                _disposition(
                    "open-item",
                    DISPOSITION_PARTIALLY_DONE,
                    amended_acceptance_criteria=["only the rest remains"],
                )
            ],
            confirmed_by="dave",
        )
        graph = load_work_graph(repo.path)
        self.assertEqual(graph[0]["acceptance_criteria"], ["only the rest remains"])
        self.assertEqual(result.applied[0]["disposition"], DISPOSITION_PARTIALLY_DONE)

    def test_invalidated_sends_item_back_to_framing(self) -> None:
        repo, _ = self._drifted()
        apply_reconcile(
            repo.path,
            [_disposition("open-item", DISPOSITION_INVALIDATED)],
            confirmed_by="dave",
        )
        graph = load_work_graph(repo.path)
        self.assertEqual(graph[0].get("status"), WORK_ITEM_STATUS_PROPOSED)

    def test_still_valid_leaves_the_graph_untouched(self) -> None:
        repo, _ = self._drifted()
        before = load_work_graph(repo.path)
        result = apply_reconcile(
            repo.path,
            [_disposition("open-item", DISPOSITION_STILL_VALID)],
            confirmed_by="dave",
        )
        self.assertEqual(load_work_graph(repo.path), before)
        self.assertEqual(result.external_completions, [])

    def test_unconfirmed_dispositions_are_not_applied(self) -> None:
        repo, _ = self._drifted()
        result = apply_reconcile(
            repo.path,
            [_disposition("open-item", DISPOSITION_INVALIDATED, confirmed=False)],
            confirmed_by="dave",
        )
        self.assertEqual(result.applied, [])
        self.assertEqual(result.skipped_unconfirmed, ["open-item"])
        # The graph is untouched and its prior status is preserved.
        self.assertIsNone(load_work_graph(repo.path)[0].get("status"))

    def test_apply_refuses_unknown_item_without_trusting_the_plan(self) -> None:
        repo, _ = self._drifted()
        with self.assertRaises(ContractError):
            apply_reconcile(
                repo.path,
                [_disposition("ghost", DISPOSITION_INVALIDATED)],
                confirmed_by="dave",
            )

    def test_apply_refuses_a_proposed_item_even_if_plan_names_it(self) -> None:
        repo = self._repo()
        repo.write_graph(
            [_item("open-item", status=WORK_ITEM_STATUS_PROPOSED)]
        )
        repo.commit("init")
        # A hand-edited plan cannot smuggle a proposed item through apply.
        with self.assertRaises(ContractError):
            apply_reconcile(
                repo.path,
                [_disposition("open-item", DISPOSITION_INVALIDATED)],
                confirmed_by="dave",
            )
        self.assertEqual(
            load_work_graph(repo.path)[0]["status"], WORK_ITEM_STATUS_PROPOSED
        )

    def test_multi_item_claim_keeps_pending_signal(self) -> None:
        repo = self._repo()
        repo.write_graph([_item("alpha"), _item("beta")])
        repo.commit("init")
        repo.write_claim("both.json", "both are done", ["alpha", "beta"])

        result = apply_reconcile(
            repo.path,
            [_disposition("alpha", DISPOSITION_INVALIDATED)],
            confirmed_by="dave",
        )
        # Only alpha was dispositioned, so the claim stays with beta pending.
        self.assertEqual(result.removed_claims, [])
        self.assertEqual(
            result.pending_claims,
            [{"filename": "both.json", "relates_to": ["alpha", "beta"], "pending": ["beta"]}],
        )
        self.assertTrue(
            (repo.path / ".agentflow" / "proposals" / "both.json").is_file()
        )

    def test_claim_naming_a_nonexistent_item_is_not_stuck_pending_forever(self) -> None:
        # A claim can name an id that no longer exists in the graph (already
        # closed by an earlier pass, or simply never a real item). Such an id
        # can never be dispositioned -- apply_reconcile refuses any disposition
        # for an unknown Work Item -- so it must not count against the claim's
        # pending signal. Otherwise a claim referencing one live item and one
        # dead id could never be resolved through reconcile: dispositioning the
        # live item would leave the claim permanently "pending" on an id no
        # human can ever confirm, defeating "one confirmation pass yields a
        # single updated graph ready for one re-approval."
        repo = self._repo()
        repo.write_graph([_item("alpha")])
        repo.commit("init")
        repo.write_claim("both.json", "alpha done; ghost already closed", ["ghost-item", "alpha"])

        result = apply_reconcile(
            repo.path,
            [_disposition("alpha", DISPOSITION_STILL_VALID)],
            confirmed_by="dave",
        )
        self.assertEqual(result.removed_claims, ["both.json"])
        self.assertEqual(result.pending_claims, [])
        self.assertFalse(
            (repo.path / ".agentflow" / "proposals" / "both.json").exists()
        )

    def test_multi_item_claim_consumed_when_all_dispositioned(self) -> None:
        repo = self._repo()
        repo.write_graph([_item("alpha"), _item("beta")])
        repo.commit("init")
        repo.write_claim("both.json", "both are done", ["alpha", "beta"])

        result = apply_reconcile(
            repo.path,
            [
                _disposition("alpha", DISPOSITION_INVALIDATED),
                _disposition("beta", DISPOSITION_STILL_VALID),
            ],
            confirmed_by="dave",
        )
        self.assertEqual(result.removed_claims, ["both.json"])

    def test_confirmed_and_unconfirmed_duplicate_for_one_item_is_rejected(self) -> None:
        # A plan naming the same work item twice -- once confirmed, once not --
        # is ambiguous about the human's decision. Applying it must not silently
        # report the item as *both* applied and skipped_unconfirmed: one
        # confirmation pass must yield a single, unambiguous outcome per item.
        repo, _ = self._drifted()
        with self.assertRaises(ContractError):
            apply_reconcile(
                repo.path,
                [
                    _disposition("open-item", DISPOSITION_STILL_VALID, confirmed=True),
                    _disposition(
                        "open-item", DISPOSITION_INVALIDATED, confirmed=False
                    ),
                ],
                confirmed_by="dave",
            )
        # Nothing was mutated by the rejected pass.
        self.assertIsNone(load_work_graph(repo.path)[0].get("status"))

    def test_apply_requires_confirmed_by(self) -> None:
        repo, _ = self._drifted()
        with self.assertRaises(ContractError):
            apply_reconcile(
                repo.path,
                [_disposition("open-item", DISPOSITION_STILL_VALID)],
                confirmed_by="  ",
            )


def _agentflow(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agentflow", *args],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )


class ReconcileCliTests(_RepoCase):
    def test_plan_then_confirmed_apply_round_trips(self) -> None:
        repo, drift_commit = self._drifted()
        repo.write_claim("done.json", "open-item is finished", ["open-item"])

        planned = _agentflow(
            "work", "reconcile", "--repository", str(repo.path), cwd=repo.path
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)
        self.assertEqual(plan["state"], "reconcile_planned")
        self.assertEqual(
            plan["dispositions"][0]["disposition"], DISPOSITION_COMPLETED_EXTERNALLY
        )

        # A human confirms the proposed disposition and applies the edited plan.
        plan["dispositions"][0]["confirmed"] = True
        plan_path = repo.path / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        applied = _agentflow(
            "work",
            "reconcile-apply",
            "--repository",
            str(repo.path),
            "--plan",
            str(plan_path),
            "--confirmed-by",
            "dave",
            cwd=repo.path,
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        result = json.loads(applied.stdout)
        self.assertEqual(result["state"], "reconciled")
        self.assertEqual(result["applied"][0]["work_item_id"], "open-item")
        self.assertEqual(load_work_graph(repo.path), [])

    def test_apply_requires_confirmed_by_flag(self) -> None:
        repo, _ = self._drifted()
        plan_path = repo.path / "plan.json"
        plan_path.write_text(json.dumps({"dispositions": []}), encoding="utf-8")
        result = _agentflow(
            "work",
            "reconcile-apply",
            "--repository",
            str(repo.path),
            "--plan",
            str(plan_path),
            cwd=repo.path,
        )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
