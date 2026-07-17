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
    MAX_DISCOVERIES_PER_OUTPUT,
    ContractError,
    validate_work_graph,
)
from agentflow.work_graph import (  # noqa: E402
    InMemoryWorkGraphBackend,
    JsonlWorkGraphBackend,
    apply_discoveries,
    compute_ready_work,
    default_work_graph_backend,
    load_work_graph,
    save_work_graph,
    work_item_content_hash,
)


def _item(item_id: str, depends_on: list[str] | None = None) -> dict:
    return {
        "id": item_id,
        "summary": f"Work {item_id}",
        "acceptance_criteria": [],
        "depends_on": depends_on or [],
    }


def run_agentflow(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agentflow", *args],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )


class WorkGraphContractTests(unittest.TestCase):
    def test_valid_graph_normalizes_defaults(self) -> None:
        graph = validate_work_graph(
            [{"id": "a", "summary": "A"}, {"id": "b", "summary": "B", "depends_on": ["a"]}]
        )
        self.assertEqual(graph[0]["depends_on"], [])
        self.assertEqual(graph[0]["acceptance_criteria"], [])
        self.assertEqual(graph[1]["depends_on"], ["a"])

    def test_duplicate_ids_rejected(self) -> None:
        with self.assertRaises(ContractError):
            validate_work_graph([_item("a"), _item("a")])

    def test_unknown_dependency_rejected(self) -> None:
        with self.assertRaises(ContractError):
            validate_work_graph([_item("a", ["ghost"])])

    def test_self_dependency_rejected(self) -> None:
        with self.assertRaises(ContractError):
            validate_work_graph([_item("a", ["a"])])

    def test_dependency_cycle_rejected(self) -> None:
        with self.assertRaises(ContractError):
            validate_work_graph([_item("a", ["b"]), _item("b", ["a"])])

    def test_unknown_field_rejected(self) -> None:
        with self.assertRaises(ContractError):
            validate_work_graph([{"id": "a", "summary": "A", "status": "done"}])


class WorkGraphBackendTests(unittest.TestCase):
    def test_jsonl_is_default_backend_for_load_and_save(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            items = [_item("a"), _item("b", ["a"])]
            saved = save_work_graph(items, repository)
            self.assertEqual([item["id"] for item in saved], ["a", "b"])
            loaded = load_work_graph(repository)
            self.assertEqual(loaded, saved)
            graph_path = repository / ".agentflow" / "work" / "graph.jsonl"
            self.assertTrue(graph_path.is_file())

    def test_swapping_backend_preserves_validation(self) -> None:
        invalid = [_item("a", ["ghost"])]
        memory = InMemoryWorkGraphBackend()
        with self.assertRaises(ContractError):
            save_work_graph(invalid, backend=memory)
        self.assertEqual(memory.read_items(), [])

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            with self.assertRaises(ContractError):
                save_work_graph(invalid, repository)
            self.assertEqual(
                JsonlWorkGraphBackend(repository).read_items(), []
            )

    def test_write_items_fully_replaces_all_jsonl_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            work_dir = repository / ".agentflow" / "work"
            work_dir.mkdir(parents=True)
            (work_dir / "alpha.jsonl").write_text(
                json.dumps(_item("old-a")) + "\n", encoding="utf-8"
            )
            (work_dir / "beta.jsonl").write_text(
                json.dumps(_item("old-b")) + "\n", encoding="utf-8"
            )
            backend = JsonlWorkGraphBackend(repository)
            replacement = validate_work_graph([_item("new")])
            backend.write_items(replacement)

            remaining = sorted(path.name for path in work_dir.glob("*.jsonl"))
            self.assertEqual(remaining, ["graph.jsonl"])
            self.assertEqual(backend.read_items(), replacement)
            self.assertEqual(load_work_graph(repository), replacement)

    def test_jsonl_and_memory_full_replace_round_trips_match(self) -> None:
        items = validate_work_graph([_item("a"), _item("b", ["a"])])
        memory = InMemoryWorkGraphBackend([_item("stale")])
        memory.write_items(items)
        memory_round_trip = memory.read_items()

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            work_dir = repository / ".agentflow" / "work"
            work_dir.mkdir(parents=True)
            (work_dir / "stale.jsonl").write_text(
                json.dumps(_item("stale")) + "\n", encoding="utf-8"
            )
            jsonl = JsonlWorkGraphBackend(repository)
            jsonl.write_items(items)
            jsonl_round_trip = jsonl.read_items()

        self.assertEqual(memory_round_trip, items)
        self.assertEqual(jsonl_round_trip, items)
        self.assertEqual(memory_round_trip, jsonl_round_trip)

        memory.write_items([])
        self.assertEqual(memory.read_items(), [])

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            jsonl = JsonlWorkGraphBackend(repository)
            jsonl.write_items(items)
            jsonl.write_items([])
            self.assertEqual(jsonl.read_items(), [])
            self.assertEqual(
                list((repository / ".agentflow" / "work").glob("*.jsonl")),
                [],
            )

    def test_swapping_backend_preserves_nested_field_isolation(self) -> None:
        """In-memory and JSONL stores must not diverge under nested mutation.

        Acceptance requires swapping the backend to change no Work Graph
        semantics. JSONL isolates via serialization; a shallow in-memory copy
        that aliases ``depends_on`` / ``acceptance_criteria`` lists does not.
        """
        items = validate_work_graph(
            [
                {
                    "id": "a",
                    "summary": "Work a",
                    "acceptance_criteria": ["keep"],
                    "depends_on": [],
                },
                _item("b", ["a"]),
            ]
        )
        expected = validate_work_graph(
            [
                {
                    "id": "a",
                    "summary": "Work a",
                    "acceptance_criteria": ["keep"],
                    "depends_on": [],
                },
                _item("b", ["a"]),
            ]
        )
        memory = InMemoryWorkGraphBackend()
        memory.write_items(items)

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            jsonl = JsonlWorkGraphBackend(repository)
            jsonl.write_items(items)

            items[0]["acceptance_criteria"].append("leaked")
            items[1]["depends_on"].append("ghost")

            memory_after_write = memory.read_items()
            jsonl_after_write = jsonl.read_items()

            memory_view = memory.read_items()
            jsonl_view = jsonl.read_items()
            memory_view[0]["acceptance_criteria"].append("read-leak")
            memory_view[1]["depends_on"].append("read-ghost")
            jsonl_view[0]["acceptance_criteria"].append("read-leak")
            jsonl_view[1]["depends_on"].append("read-ghost")

            memory_after_read = memory.read_items()
            jsonl_after_read = jsonl.read_items()

        self.assertEqual(jsonl_after_write, expected)
        self.assertEqual(memory_after_write, expected)
        self.assertEqual(memory_after_write, jsonl_after_write)
        self.assertEqual(jsonl_after_read, expected)
        self.assertEqual(memory_after_read, expected)
        self.assertEqual(memory_after_read, jsonl_after_read)

    def test_save_through_memory_backend_isolates_returned_items(self) -> None:
        """save_work_graph via in-memory must not alias nested fields in store."""
        memory = InMemoryWorkGraphBackend()
        returned = save_work_graph(
            [
                {
                    "id": "a",
                    "summary": "Work a",
                    "acceptance_criteria": ["keep"],
                    "depends_on": [],
                }
            ],
            backend=memory,
        )
        returned[0]["acceptance_criteria"].append("leaked")
        loaded = load_work_graph(backend=memory)
        self.assertEqual(loaded[0]["acceptance_criteria"], ["keep"])

    def test_save_work_graph_fully_replaces_existing_jsonl_set(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            work_dir = repository / ".agentflow" / "work"
            work_dir.mkdir(parents=True)
            (work_dir / "alpha.jsonl").write_text(
                json.dumps(_item("old-a")) + "\n", encoding="utf-8"
            )
            (work_dir / "beta.jsonl").write_text(
                json.dumps(_item("old-b")) + "\n", encoding="utf-8"
            )
            replacement = save_work_graph([_item("new")], repository)
            remaining = sorted(path.name for path in work_dir.glob("*.jsonl"))
            self.assertEqual(remaining, ["graph.jsonl"])
            self.assertEqual(load_work_graph(repository), replacement)

    def test_swapping_backend_preserves_ready_work_semantics(self) -> None:
        graph = [_item("a"), _item("b", ["a"]), _item("c", ["b"])]
        memory = InMemoryWorkGraphBackend()
        saved_memory = save_work_graph(graph, backend=memory)
        ready_memory = [
            item["id"]
            for item in compute_ready_work(load_work_graph(backend=memory), set())
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            saved_jsonl = save_work_graph(graph, repository)
            ready_jsonl = [
                item["id"]
                for item in compute_ready_work(load_work_graph(repository), set())
            ]

        self.assertEqual(saved_memory, saved_jsonl)
        self.assertEqual(ready_memory, ["a"])
        self.assertEqual(ready_jsonl, ["a"])
        self.assertEqual(ready_memory, ready_jsonl)

    def test_default_work_graph_backend_is_jsonl_for_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            backend = default_work_graph_backend(repository)
            self.assertIsInstance(backend, JsonlWorkGraphBackend)
            save_work_graph([_item("a")], repository)
            self.assertEqual(backend.read_items(), load_work_graph(repository))

    def test_load_and_save_route_through_injected_backend(self) -> None:
        """Reads and writes must go through the backend interface, not a side path."""

        class RecordingBackend:
            def __init__(self) -> None:
                self.reads = 0
                self.writes: list[list[dict]] = []
                self._items: list[dict] = []

            def read_items(self) -> list[dict]:
                self.reads += 1
                return [dict(item) for item in self._items]

            def write_items(self, items: list[dict]) -> None:
                self.writes.append([dict(item) for item in items])
                self._items = [dict(item) for item in items]

        backend = RecordingBackend()
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            saved = save_work_graph(
                [_item("a"), _item("b", ["a"])], repository, backend=backend
            )
            loaded = load_work_graph(repository, backend=backend)
            self.assertEqual(len(backend.writes), 1)
            self.assertEqual(backend.reads, 1)
            self.assertEqual(saved, loaded)
            self.assertEqual([item["id"] for item in backend.writes[0]], ["a", "b"])
            self.assertFalse((repository / ".agentflow" / "work").exists())

    def test_invalid_save_does_not_write_and_preserves_prior_graph(self) -> None:
        prior = [_item("ok")]
        invalid = [_item("a", ["ghost"])]
        memory = InMemoryWorkGraphBackend()
        save_work_graph(prior, backend=memory)
        with self.assertRaises(ContractError):
            save_work_graph(invalid, backend=memory)
        self.assertEqual(load_work_graph(backend=memory), validate_work_graph(prior))

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            save_work_graph(prior, repository)
            with self.assertRaises(ContractError):
                save_work_graph(invalid, repository)
            self.assertEqual(load_work_graph(repository), validate_work_graph(prior))

    def test_load_rejects_invalid_planted_items_on_both_backends(self) -> None:
        invalid = [
            {
                "id": "a",
                "summary": "A",
                "acceptance_criteria": [],
                "depends_on": ["ghost"],
            }
        ]
        memory = InMemoryWorkGraphBackend()
        memory.write_items(invalid)
        with self.assertRaises(ContractError) as memory_error:
            load_work_graph(backend=memory)

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            JsonlWorkGraphBackend(repository).write_items(invalid)
            with self.assertRaises(ContractError) as jsonl_error:
                load_work_graph(repository)

        self.assertEqual(str(memory_error.exception), str(jsonl_error.exception))

    def test_inmemory_constructor_isolates_nested_fields(self) -> None:
        source = [
            {
                "id": "a",
                "summary": "Work a",
                "acceptance_criteria": ["keep"],
                "depends_on": [],
            }
        ]
        memory = InMemoryWorkGraphBackend(source)
        source[0]["acceptance_criteria"].append("leaked")
        source[0]["depends_on"].append("ghost")
        self.assertEqual(
            memory.read_items()[0]["acceptance_criteria"], ["keep"]
        )
        self.assertEqual(memory.read_items()[0]["depends_on"], [])

    def test_jsonl_save_return_isolates_from_store(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            returned = save_work_graph(
                [
                    {
                        "id": "a",
                        "summary": "Work a",
                        "acceptance_criteria": ["keep"],
                        "depends_on": [],
                    }
                ],
                repository,
            )
            returned[0]["acceptance_criteria"].append("leaked")
            loaded = load_work_graph(repository)
        self.assertEqual(loaded[0]["acceptance_criteria"], ["keep"])

    def test_write_items_replaces_dotfile_jsonl_and_preserves_non_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            work_dir = repository / ".agentflow" / "work"
            work_dir.mkdir(parents=True)
            (work_dir / ".stale.jsonl").write_text(
                json.dumps(_item("hidden-stale")) + "\n", encoding="utf-8"
            )
            (work_dir / "notes.md").write_text("keep me\n", encoding="utf-8")
            (work_dir / "alpha.jsonl").write_text(
                json.dumps(_item("old")) + "\n", encoding="utf-8"
            )
            backend = JsonlWorkGraphBackend(repository)
            replacement = validate_work_graph([_item("new")])
            backend.write_items(replacement)

            remaining_jsonl = sorted(path.name for path in work_dir.glob("*.jsonl"))
            self.assertEqual(remaining_jsonl, ["graph.jsonl"])
            self.assertFalse((work_dir / ".stale.jsonl").exists())
            self.assertEqual(
                (work_dir / "notes.md").read_text(encoding="utf-8"), "keep me\n"
            )
            self.assertEqual(backend.read_items(), replacement)
            memory = InMemoryWorkGraphBackend([_item("hidden-stale"), _item("old")])
            memory.write_items(replacement)
            self.assertEqual(memory.read_items(), backend.read_items())


def _discovery(key: str, depends_on: list[str] | None = None) -> dict:
    return {
        "key": key,
        "summary": f"Discovered {key}",
        "acceptance_criteria": [],
        "depends_on": depends_on or [],
    }


class ApplyDiscoveriesTests(unittest.TestCase):
    def test_applies_discoveries_as_proposed_items_via_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            save_work_graph([_item("a")], repository)
            result = apply_discoveries(
                [_discovery("found-x", ["a"]), _discovery("found-y")],
                repository,
            )
            self.assertEqual(result.applied, ["found-x", "found-y"])
            self.assertEqual(result.skipped_existing, [])
            self.assertEqual(result.skipped_unresolved, [])
            graph = load_work_graph(repository)
            by_id = {item["id"]: item for item in graph}
            self.assertEqual(by_id["found-x"]["status"], "proposed")
            self.assertEqual(by_id["found-x"]["depends_on"], ["a"])
            self.assertNotIn("status", by_id["a"])

    def test_reapplication_is_idempotent(self) -> None:
        memory = InMemoryWorkGraphBackend()
        save_work_graph([_item("a")], backend=memory)
        discoveries = [_discovery("found-x", ["a"])]
        first = apply_discoveries(discoveries, backend=memory)
        graph_after_first = load_work_graph(backend=memory)
        second = apply_discoveries(discoveries, backend=memory)
        self.assertEqual(first.applied, ["found-x"])
        self.assertEqual(second.applied, [])
        self.assertEqual(second.skipped_existing, ["found-x"])
        self.assertEqual(load_work_graph(backend=memory), graph_after_first)

    def test_duplicate_keys_against_existing_items_are_dropped(self) -> None:
        memory = InMemoryWorkGraphBackend()
        save_work_graph([_item("a")], backend=memory)
        result = apply_discoveries(
            [_discovery("a"), _discovery("found-new")], backend=memory
        )
        self.assertEqual(result.applied, ["found-new"])
        self.assertEqual(result.skipped_existing, ["a"])
        graph = load_work_graph(backend=memory)
        self.assertEqual([item["id"] for item in graph], ["a", "found-new"])
        # The existing item is untouched, not overwritten by the discovery.
        self.assertEqual(graph[0]["summary"], "Work a")

    def test_unresolved_and_cyclic_dependencies_are_dropped(self) -> None:
        memory = InMemoryWorkGraphBackend()
        save_work_graph([_item("a")], backend=memory)
        result = apply_discoveries(
            [
                _discovery("ghost-dep", ["missing"]),
                _discovery("cycle-1", ["cycle-2"]),
                _discovery("cycle-2", ["cycle-1"]),
                _discovery("fine", ["a"]),
            ],
            backend=memory,
        )
        self.assertEqual(result.applied, ["fine"])
        self.assertEqual(
            result.skipped_unresolved, ["ghost-dep", "cycle-1", "cycle-2"]
        )
        self.assertEqual(
            [item["id"] for item in load_work_graph(backend=memory)],
            ["a", "fine"],
        )

    def test_batch_dependencies_resolve_regardless_of_order(self) -> None:
        memory = InMemoryWorkGraphBackend()
        result = apply_discoveries(
            [_discovery("later-first", ["later-second"]), _discovery("later-second")],
            backend=memory,
        )
        self.assertEqual(sorted(result.applied), ["later-first", "later-second"])
        self.assertEqual(result.skipped_unresolved, [])
        self.assertEqual(len(load_work_graph(backend=memory)), 2)

    def test_over_cap_and_duplicate_key_batches_are_rejected(self) -> None:
        memory = InMemoryWorkGraphBackend()
        over_cap = [
            _discovery(f"found-{index}")
            for index in range(MAX_DISCOVERIES_PER_OUTPUT + 1)
        ]
        with self.assertRaisesRegex(ContractError, "at most"):
            apply_discoveries(over_cap, backend=memory)
        with self.assertRaisesRegex(ContractError, "duplicate keys"):
            apply_discoveries(
                [_discovery("same"), _discovery("same")], backend=memory
            )
        self.assertEqual(load_work_graph(backend=memory), [])

    def test_no_admitted_discoveries_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            result = apply_discoveries(
                [_discovery("ghost", ["missing"])], repository
            )
            self.assertEqual(result.applied, [])
            self.assertFalse((repository / ".agentflow" / "work").exists())

    def test_proposed_items_are_excluded_from_ready_work(self) -> None:
        memory = InMemoryWorkGraphBackend()
        save_work_graph([_item("a")], backend=memory)
        apply_discoveries([_discovery("found-x")], backend=memory)
        graph = load_work_graph(backend=memory)
        self.assertEqual(
            [item["id"] for item in compute_ready_work(graph, set())], ["a"]
        )
        # A human approving the proposal (removing the marker) makes it ready.
        approved = [
            {key: value for key, value in item.items() if key != "status"}
            for item in graph
        ]
        self.assertEqual(
            [item["id"] for item in compute_ready_work(approved, {"a"})],
            ["found-x"],
        )

    def test_backend_swap_preserves_application_semantics(self) -> None:
        discoveries = [_discovery("found-x"), _discovery("found-y", ["found-x"])]
        memory = InMemoryWorkGraphBackend()
        save_work_graph([_item("a")], backend=memory)
        memory_result = apply_discoveries(discoveries, backend=memory)

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            save_work_graph([_item("a")], repository)
            jsonl_result = apply_discoveries(discoveries, repository)
            jsonl_graph = load_work_graph(repository)

        self.assertEqual(memory_result.applied, jsonl_result.applied)
        self.assertEqual(load_work_graph(backend=memory), jsonl_graph)


class ReadyWorkTests(unittest.TestCase):
    def test_ready_excludes_completed_and_blocked(self) -> None:
        graph = validate_work_graph(
            [_item("a"), _item("b", ["a"]), _item("c", ["b"])]
        )
        # Nothing done: only the dependency-free item is ready.
        self.assertEqual(
            [item["id"] for item in compute_ready_work(graph, set())], ["a"]
        )
        # a done: b unblocks; c still blocked by b.
        self.assertEqual(
            [item["id"] for item in compute_ready_work(graph, {"a"})], ["b"]
        )
        # a and b done: c ready, a/b excluded as completed.
        self.assertEqual(
            [item["id"] for item in compute_ready_work(graph, {"a", "b"})], ["c"]
        )

    def test_content_hash_is_stable_and_sensitive(self) -> None:
        item = _item("a")
        self.assertEqual(
            work_item_content_hash(item), work_item_content_hash(_item("a"))
        )
        self.assertNotEqual(
            work_item_content_hash(item),
            work_item_content_hash({**item, "summary": "changed"}),
        )


class WorkCommandTests(unittest.TestCase):
    def test_work_list_and_ready_reflect_completed_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "repo"
            data_dir = temp_path / "home"
            work_dir = repository / ".agentflow" / "work"
            work_dir.mkdir(parents=True)
            (work_dir / "graph.jsonl").write_text(
                "\n".join(
                    json.dumps(item)
                    for item in [_item("a"), _item("b", ["a"])]
                )
                + "\n",
                encoding="utf-8",
            )

            listed = run_agentflow(
                "work", "list", "--repository", str(repository), cwd=temp_path
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(
                [item["id"] for item in json.loads(listed.stdout)], ["a", "b"]
            )

            ready = run_agentflow(
                "work",
                "ready",
                "--repository",
                str(repository),
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
            )
            self.assertEqual(ready.returncode, 0, ready.stderr)
            self.assertEqual(
                [item["id"] for item in json.loads(ready.stdout)], ["a"]
            )

            # Record a human_approved Run that captured work item "a", so "a"
            # counts as completed and "b" becomes ready.
            content_hash = work_item_content_hash(_item("a"))
            run_id = "run-a"
            run_dir = data_dir / "runs" / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "task.json").write_text(
                json.dumps(
                    {
                        "summary": "deliver a",
                        "acceptance_criteria": [],
                        "source": {
                            "provider": "work-graph",
                            "work_item_id": "a",
                            "captured_at": "2026-07-16T00:00:00+00:00",
                            "content_hash": content_hash,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "events.jsonl").write_text(
                "".join(
                    json.dumps(event) + "\n"
                    for event in [
                        {"run_id": run_id, "sequence": 1, "type": "run_created"},
                        {
                            "approved_by": "d",
                            "approved_sha": "0" * 40,
                            "candidate_sha": "0" * 40,
                            "sequence": 2,
                            "type": "human_approved",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            ready_after = run_agentflow(
                "work",
                "ready",
                "--repository",
                str(repository),
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
            )
            self.assertEqual(ready_after.returncode, 0, ready_after.stderr)
            self.assertEqual(
                [item["id"] for item in json.loads(ready_after.stdout)], ["b"]
            )


class CaptureWorkItemTests(unittest.TestCase):
    def _init_repo(self, repository: Path) -> None:
        repository.mkdir(parents=True)
        subprocess.run(
            ["git", "init"], cwd=repository, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.email", "agentflow@example.test"],
            cwd=repository,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Agentflow Test"],
            cwd=repository,
            check=True,
        )
        (repository / "README.md").write_text("# Target\n", encoding="utf-8")
        work_dir = repository / ".agentflow" / "work"
        work_dir.mkdir(parents=True)
        (work_dir / "graph.jsonl").write_text(
            "\n".join(
                json.dumps(item)
                for item in [
                    {
                        "id": "health",
                        "summary": "Add a health endpoint",
                        "acceptance_criteria": ["GET /health returns 200"],
                        "depends_on": [],
                    }
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Init with work graph"],
            cwd=repository,
            check=True,
            capture_output=True,
        )

    def test_start_work_item_captures_summary_criteria_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "repo"
            data_dir = temp_path / "home"
            self._init_repo(repository)
            environment = {
                **os.environ,
                "PYTHONPATH": str(PROJECT_ROOT / "src"),
            }

            started = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentflow",
                    "start",
                    "--work-item",
                    "health",
                    "--data-dir",
                    str(data_dir),
                ],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            run_id = json.loads(started.stdout)["run_id"]
            self.assertEqual(json.loads(started.stdout)["work_item_id"], "health")

            status = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentflow",
                    "status",
                    run_id,
                    "--data-dir",
                    str(data_dir),
                ],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            payload = json.loads(status.stdout)
            self.assertEqual(payload["summary"], "Add a health endpoint")
            self.assertEqual(
                payload["acceptance_criteria"], ["GET /health returns 200"]
            )
            source = payload["source"]
            self.assertEqual(source["provider"], "work-graph")
            self.assertEqual(source["work_item_id"], "health")
            expected_hash = work_item_content_hash(
                {
                    "id": "health",
                    "summary": "Add a health endpoint",
                    "acceptance_criteria": ["GET /health returns 200"],
                    "depends_on": [],
                }
            )
            self.assertEqual(source["content_hash"], expected_hash)

    def test_start_unknown_work_item_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "repo"
            self._init_repo(repository)
            environment = {
                **os.environ,
                "PYTHONPATH": str(PROJECT_ROOT / "src"),
            }
            started = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentflow",
                    "start",
                    "--work-item",
                    "ghost",
                    "--data-dir",
                    str(temp_path / "home"),
                ],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(started.returncode, 0)
            self.assertIn("ghost", started.stderr)


if __name__ == "__main__":
    unittest.main()
