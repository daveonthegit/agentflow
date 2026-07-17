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
    MAX_PROPOSALS_PER_INGEST,
    WORK_ITEM_STATUS_PROPOSED,
    proposal_work_item_id,
)
from agentflow.proposals import (  # noqa: E402
    ingest_proposals,
    scan_proposals,
)
from agentflow.work_graph import (  # noqa: E402
    completed_work_item_ids,
    compute_ready_work,
    load_work_graph,
    save_work_graph,
    work_graph_content_hash,
)


def run_agentflow(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agentflow", *args],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )


def _write_proposal(repository: Path, filename: str, payload: dict | str) -> Path:
    directory = repository / ".agentflow" / "proposals"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return path


def _new_work(summary: str, criteria: list[str] | None = None) -> dict:
    return {
        "kind": "new-work",
        "summary": summary,
        "acceptance_criteria": criteria or ["it works"],
    }


class ScanInboxTests(unittest.TestCase):
    def test_missing_inbox_is_empty_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scanned = scan_proposals(Path(temp_dir))
            self.assertEqual(scanned.valid, [])
            self.assertEqual(scanned.invalid, [])

    def test_valid_and_invalid_files_are_separated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            _write_proposal(repository, "a.json", _new_work("first"))
            _write_proposal(
                repository,
                "b.json",
                {"kind": "completion-claim", "summary": "done", "relates_to": ["x"]},
            )
            _write_proposal(repository, "c.json", "{not json")
            _write_proposal(
                repository, "d.json", {"kind": "bug", "summary": "bad kind"}
            )

            scanned = scan_proposals(repository)
            self.assertEqual(
                [p.filename for p in scanned.valid], ["a.json", "b.json"]
            )
            self.assertEqual(
                [p.kind for p in scanned.valid], ["new-work", "completion-claim"]
            )
            # new-work carries a derived id, completion-claim does not.
            self.assertIsNotNone(scanned.valid[0].work_item_id)
            self.assertIsNone(scanned.valid[1].work_item_id)
            self.assertEqual(
                sorted(entry["filename"] for entry in scanned.invalid),
                ["c.json", "d.json"],
            )

    def test_invalid_file_never_aborts_scan_of_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            _write_proposal(repository, "01-bad.json", "garbage")
            _write_proposal(repository, "02-good.json", _new_work("survivor"))
            scanned = scan_proposals(repository)
            self.assertEqual([p.summary for p in scanned.valid], ["survivor"])
            self.assertEqual(len(scanned.invalid), 1)


class IngestTests(unittest.TestCase):
    def test_new_work_becomes_proposed_items_with_stable_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            _write_proposal(repository, "a.json", _new_work("add caching"))
            expected_id = proposal_work_item_id(_new_work("add caching"))

            result = ingest_proposals(repository)
            self.assertEqual(result.applied, [expected_id])
            graph = load_work_graph(repository)
            item = next(entry for entry in graph if entry["id"] == expected_id)
            self.assertEqual(item["status"], WORK_ITEM_STATUS_PROPOSED)
            self.assertEqual(item["summary"], "add caching")
            self.assertEqual(item["depends_on"], [])
            # The consumed file is removed.
            self.assertEqual(result.removed, ["a.json"])
            self.assertFalse((repository / ".agentflow" / "proposals" / "a.json").exists())

    def test_ingest_is_idempotent_on_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            _write_proposal(repository, "a.json", _new_work("thing"))
            first = ingest_proposals(repository)
            self.assertEqual(len(first.applied), 1)
            # Re-running finds an empty inbox: nothing applied, graph unchanged.
            second = ingest_proposals(repository)
            self.assertEqual(second.applied, [])
            self.assertEqual(
                [e["id"] for e in load_work_graph(repository)], first.applied
            )

    def test_redropped_proposal_dedups_against_graph_and_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            _write_proposal(repository, "a.json", _new_work("thing"))
            ingest_proposals(repository)
            # Same content dropped again under a new filename.
            _write_proposal(repository, "again.json", _new_work("thing"))
            result = ingest_proposals(repository)
            self.assertEqual(result.applied, [])
            self.assertEqual(result.skipped_existing, [proposal_work_item_id(_new_work("thing"))])
            self.assertEqual(result.removed, ["again.json"])
            # Graph still has exactly one item.
            self.assertEqual(len(load_work_graph(repository)), 1)

    def test_duplicate_proposals_in_one_pass_are_deduped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            _write_proposal(repository, "a.json", _new_work("same"))
            _write_proposal(repository, "b.json", _new_work("same"))
            result = ingest_proposals(repository)
            derived = proposal_work_item_id(_new_work("same"))
            self.assertEqual(result.applied, [derived])
            self.assertEqual(result.skipped_duplicate, [derived])
            self.assertEqual(sorted(result.removed), ["a.json", "b.json"])
            self.assertEqual(len(load_work_graph(repository)), 1)

    def test_dedups_against_existing_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            derived = proposal_work_item_id(_new_work("preexisting"))
            save_work_graph(
                [
                    {
                        "id": derived,
                        "summary": "preexisting",
                        "acceptance_criteria": [],
                        "depends_on": [],
                    }
                ],
                repository,
            )
            _write_proposal(repository, "a.json", _new_work("preexisting"))
            result = ingest_proposals(repository)
            self.assertEqual(result.applied, [])
            self.assertEqual(result.skipped_existing, [derived])
            self.assertEqual(len(load_work_graph(repository)), 1)

    def test_over_cap_proposals_are_deferred_not_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            total = MAX_PROPOSALS_PER_INGEST + 3
            for index in range(total):
                _write_proposal(
                    repository, f"p{index:02d}.json", _new_work(f"work {index}")
                )
            result = ingest_proposals(repository)
            self.assertEqual(len(result.applied), MAX_PROPOSALS_PER_INGEST)
            self.assertEqual(len(result.skipped_over_cap), 3)
            # Over-cap files remain in the inbox for a later pass.
            remaining = list((repository / ".agentflow" / "proposals").glob("*.json"))
            self.assertEqual(len(remaining), 3)
            # A second pass drains the remainder deterministically.
            second = ingest_proposals(repository)
            self.assertEqual(len(second.applied), 3)
            self.assertEqual(len(load_work_graph(repository)), total)

    def test_invalid_files_reported_left_in_place_never_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            _write_proposal(repository, "good.json", _new_work("valid one"))
            _write_proposal(repository, "bad.json", "{broken")
            result = ingest_proposals(repository)
            self.assertEqual(len(result.applied), 1)
            self.assertEqual(
                [entry["filename"] for entry in result.invalid], ["bad.json"]
            )
            # The invalid file is left for its author to fix.
            self.assertTrue(
                (repository / ".agentflow" / "proposals" / "bad.json").exists()
            )


class CompletionClaimTests(unittest.TestCase):
    def test_completion_claim_surfaces_but_never_mutates_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            _write_proposal(
                repository,
                "claim.json",
                {
                    "kind": "completion-claim",
                    "summary": "already done in def456",
                    "relates_to": ["health"],
                },
            )
            before = work_graph_content_hash(load_work_graph(repository))
            result = ingest_proposals(repository)
            self.assertEqual(result.applied, [])
            self.assertEqual(len(result.completion_claims), 1)
            self.assertEqual(result.completion_claims[0]["relates_to"], ["health"])
            # Never applied and the file is left in place for reconcile.
            self.assertEqual(result.removed, [])
            self.assertTrue(
                (repository / ".agentflow" / "proposals" / "claim.json").exists()
            )
            after = work_graph_content_hash(load_work_graph(repository))
            self.assertEqual(before, after)


class TrustBoundaryTests(unittest.TestCase):
    def test_ingest_only_mints_proposed_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            _write_proposal(repository, "a.json", _new_work("one"))
            _write_proposal(repository, "b.json", _new_work("two"))
            ingest_proposals(repository)
            graph = load_work_graph(repository)
            self.assertTrue(graph)
            for item in graph:
                self.assertEqual(item.get("status"), WORK_ITEM_STATUS_PROPOSED)

    def test_ingested_items_are_excluded_from_ready_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            data_dir = Path(temp_dir) / "home"
            _write_proposal(repository, "a.json", _new_work("proposed work"))
            ingest_proposals(repository)
            graph = load_work_graph(repository)
            ready = compute_ready_work(graph, completed_work_item_ids(data_dir))
            self.assertEqual(ready, [])

    def test_proposal_files_do_not_affect_graph_approval_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            save_work_graph(
                [
                    {
                        "id": "real",
                        "summary": "real work",
                        "acceptance_criteria": [],
                        "depends_on": [],
                    }
                ],
                repository,
            )
            before = work_graph_content_hash(load_work_graph(repository))
            _write_proposal(repository, "a.json", _new_work("noise"))
            _write_proposal(
                repository,
                "b.json",
                {"kind": "completion-claim", "summary": "claim"},
            )
            after = work_graph_content_hash(load_work_graph(repository))
            self.assertEqual(before, after)


class ProposalCommandTests(unittest.TestCase):
    def test_work_proposals_lists_without_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "repo"
            repository.mkdir()
            _write_proposal(repository, "a.json", _new_work("listed"))
            _write_proposal(repository, "b.json", "{bad")

            listed = run_agentflow(
                "work", "proposals", "--repository", str(repository), cwd=temp_path
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            payload = json.loads(listed.stdout)
            self.assertEqual([p["filename"] for p in payload["valid"]], ["a.json"])
            self.assertEqual(
                [entry["filename"] for entry in payload["invalid"]], ["b.json"]
            )
            # Nothing was ingested or removed.
            self.assertTrue(
                (repository / ".agentflow" / "proposals" / "a.json").exists()
            )
            self.assertEqual(load_work_graph(repository), [])

    def test_work_ingest_applies_new_work_and_surfaces_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "repo"
            repository.mkdir()
            _write_proposal(repository, "a.json", _new_work("ship it"))
            _write_proposal(
                repository,
                "claim.json",
                {"kind": "completion-claim", "summary": "already done"},
            )

            ingested = run_agentflow(
                "work", "ingest", "--repository", str(repository), cwd=temp_path
            )
            self.assertEqual(ingested.returncode, 0, ingested.stderr)
            payload = json.loads(ingested.stdout)
            self.assertEqual(len(payload["applied"]), 1)
            self.assertEqual(len(payload["completion_claims"]), 1)
            self.assertEqual(payload["removed"], ["a.json"])

            graph = load_work_graph(repository)
            self.assertEqual(len(graph), 1)
            self.assertEqual(graph[0]["status"], WORK_ITEM_STATUS_PROPOSED)
            # Completion-claim file remains for reconcile.
            self.assertTrue(
                (repository / ".agentflow" / "proposals" / "claim.json").exists()
            )


if __name__ == "__main__":
    unittest.main()
