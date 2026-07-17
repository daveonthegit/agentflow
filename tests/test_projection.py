from __future__ import annotations

import ast
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from agentflow.projection import build_projection, read_events_tolerant  # noqa: E402
from agentflow.work_graph import approve_work_graph, save_work_graph  # noqa: E402


def run_agentflow(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agentflow", *args],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(SRC_ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def init_repository(path: Path) -> str:
    path.mkdir(parents=True)
    git("init", cwd=path)
    git("config", "user.email", "agentflow@example.test", cwd=path)
    git("config", "user.name", "Agentflow Test", cwd=path)
    (path / "README.md").write_text("# Target\n", encoding="utf-8")
    git("add", "README.md", cwd=path)
    git("commit", "-m", "Initial commit", cwd=path)
    return git("rev-parse", "HEAD", cwd=path)


def write_events(run_dir: Path, payload: bytes | str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "events.jsonl"
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def commit_work_graph(repository: Path, items: list[dict]) -> None:
    save_work_graph(items, repository)
    git("add", "-A", "-f", cwd=repository)
    git("commit", "-m", "work graph", cwd=repository)


def approve_graph(repository: Path, data_dir: Path) -> None:
    """Approve the Work Graph so `start --work-item` may capture from it."""
    approve_work_graph(
        repository=repository, data_dir=data_dir, approved_by="tester"
    )


class ProjectionRebuildTests(unittest.TestCase):
    def test_projection_renders_runs_work_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "target"
            data_dir = temp_path / "home"
            init_repository(repository)
            commit_work_graph(
                repository,
                [
                    {
                        "id": "alpha",
                        "summary": "First",
                        "acceptance_criteria": ["a"],
                        "depends_on": [],
                    },
                    {
                        "id": "beta",
                        "summary": "Second",
                        "acceptance_criteria": ["b"],
                        "depends_on": ["alpha"],
                    },
                ],
            )
            approve_graph(repository, data_dir)
            started = run_agentflow(
                "start",
                "--work-item",
                "alpha",
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            run_id = json.loads(started.stdout)["run_id"]

            projected = run_agentflow(
                "project",
                "--repository",
                str(repository),
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )
            self.assertEqual(projected.returncode, 0, projected.stderr)
            body = json.loads(projected.stdout)
            self.assertEqual(set(body), {"runs", "work", "evidence"})
            self.assertEqual(len(body["runs"]), 1)
            self.assertEqual(body["runs"][0]["run_id"], run_id)
            self.assertEqual(body["runs"][0]["state"], "ready")
            self.assertEqual(body["runs"][0]["work_item_id"], "alpha")
            self.assertEqual(
                [item["id"] for item in body["work"]["items"]],
                ["alpha", "beta"],
            )
            self.assertEqual(
                [item["id"] for item in body["work"]["ready"]],
                ["alpha"],
            )
            self.assertEqual(body["work"]["completed_ids"], [])
            self.assertEqual(len(body["evidence"]), 1)
            self.assertEqual(body["evidence"][0]["run_id"], run_id)
            self.assertGreaterEqual(len(body["evidence"][0]["events"]), 1)
            self.assertFalse(body["evidence"][0]["truncated"])

    def test_projection_is_rebuildable_from_events_identically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "target"
            data_dir = temp_path / "home"
            init_repository(repository)
            commit_work_graph(
                repository,
                [
                    {
                        "id": "alpha",
                        "summary": "First",
                        "acceptance_criteria": [],
                        "depends_on": [],
                    }
                ],
            )
            started = run_agentflow(
                "start",
                "Rebuild me",
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )
            self.assertEqual(started.returncode, 0, started.stderr)

            first = build_projection(data_dir=data_dir, repository=repository)
            second = build_projection(data_dir=data_dir, repository=repository)
            self.assertEqual(first, second)
            # Rebuilding never writes into Agentflow Home or the repository.
            before = {
                path.relative_to(temp_path): path.read_bytes()
                for path in temp_path.rglob("*")
                if path.is_file()
            }
            build_projection(data_dir=data_dir, repository=repository)
            after = {
                path.relative_to(temp_path): path.read_bytes()
                for path in temp_path.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)

    def test_projection_rereads_events_on_each_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "target"
            data_dir = temp_path / "home"
            init_repository(repository)
            save_work_graph([], repository)
            run_dir = data_dir / "runs" / "run-live"
            write_events(
                run_dir,
                json.dumps(
                    {"run_id": "run-live", "sequence": 1, "type": "run_created"}
                )
                + "\n",
            )
            (run_dir / "task.json").write_text(
                json.dumps({"summary": "Live"}),
                encoding="utf-8",
            )
            (run_dir / "repository.json").write_text(
                json.dumps(
                    {"repository": str(repository), "base_sha": "base"}
                ),
                encoding="utf-8",
            )

            first = build_projection(data_dir=data_dir, repository=repository)
            self.assertEqual(first["runs"][0]["state"], "created")

            with (run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "run_id": "run-live",
                            "sequence": 2,
                            "type": "build_ready",
                            "candidate_sha": "cand",
                        }
                    )
                    + "\n"
                )

            second = build_projection(data_dir=data_dir, repository=repository)
            self.assertNotEqual(first, second)
            self.assertEqual(second["runs"][0]["state"], "built")
            self.assertEqual(second["runs"][0]["candidate_sha"], "cand")
            self.assertEqual(len(second["evidence"][0]["events"]), 2)

    def test_project_cli_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "target"
            data_dir = temp_path / "home"
            init_repository(repository)
            commit_work_graph(
                repository,
                [
                    {
                        "id": "alpha",
                        "summary": "First",
                        "acceptance_criteria": ["a"],
                        "depends_on": [],
                    }
                ],
            )
            approve_graph(repository, data_dir)
            started = run_agentflow(
                "start",
                "--work-item",
                "alpha",
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            before = {
                path.relative_to(temp_path): path.read_bytes()
                for path in temp_path.rglob("*")
                if path.is_file()
            }
            projected = run_agentflow(
                "project",
                "--repository",
                str(repository),
                "--data-dir",
                str(data_dir),
                cwd=repository,
            )
            self.assertEqual(projected.returncode, 0, projected.stderr)
            after = {
                path.relative_to(temp_path): path.read_bytes()
                for path in temp_path.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)

    def test_human_approved_run_drives_work_completion_from_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "target"
            data_dir = temp_path / "home"
            init_repository(repository)
            save_work_graph(
                [
                    {
                        "id": "alpha",
                        "summary": "First",
                        "acceptance_criteria": ["a"],
                        "depends_on": [],
                    },
                    {
                        "id": "beta",
                        "summary": "Second",
                        "acceptance_criteria": ["b"],
                        "depends_on": ["alpha"],
                    },
                ],
                repository,
            )
            run_dir = data_dir / "runs" / "run-alpha"
            write_events(
                run_dir,
                "".join(
                    json.dumps(event) + "\n"
                    for event in (
                        {
                            "run_id": "run-alpha",
                            "sequence": 1,
                            "type": "run_created",
                        },
                        {
                            "run_id": "run-alpha",
                            "sequence": 2,
                            "type": "human_approved",
                            "approved_sha": "approved",
                        },
                    )
                ),
            )
            (run_dir / "task.json").write_text(
                json.dumps(
                    {
                        "summary": "First",
                        "source": {
                            "provider": "work-graph",
                            "work_item_id": "alpha",
                            "content_hash": "hash",
                            "captured_at": "2026-07-16T00:00:00+00:00",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "repository.json").write_text(
                json.dumps(
                    {"repository": str(repository), "base_sha": "base"}
                ),
                encoding="utf-8",
            )

            projection = build_projection(
                data_dir=data_dir, repository=repository
            )
            self.assertEqual(projection["work"]["completed_ids"], ["alpha"])
            self.assertEqual(
                [item["id"] for item in projection["work"]["ready"]],
                ["beta"],
            )
            self.assertEqual(
                [item["id"] for item in projection["work"]["items"]],
                ["alpha", "beta"],
            )


class ProjectionAuthorityTests(unittest.TestCase):
    def test_workflow_mutators_do_not_import_projection(self) -> None:
        package = SRC_ROOT / "agentflow"
        for relative in (
            "workflow.py",
            "run_kernel.py",
            "reconcile.py",
            "work_graph.py",
        ):
            imports = _imported_modules(package / relative)
            self.assertNotIn(
                "projection",
                imports,
                f"{relative} must not import the projection module",
            )
            self.assertNotIn(
                "agentflow.projection",
                imports,
                f"{relative} must not import the projection module",
            )

    def test_start_advance_approve_do_not_call_build_projection(self) -> None:
        main_source = (SRC_ROOT / "agentflow" / "__main__.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(main_source)

        class CommandVisitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.command_calls: dict[str, set[str]] = {}

            def visit_If(self, node: ast.If) -> None:
                command = self._command_name(node.test)
                if command in {"start", "advance", "approve"}:
                    names: set[str] = set()
                    for child in ast.walk(node):
                        if isinstance(child, ast.Call):
                            names.add(self._call_name(child.func))
                    self.command_calls[command] = names
                self.generic_visit(node)

            @staticmethod
            def _command_name(test: ast.AST) -> str | None:
                if not isinstance(test, ast.Compare) or len(test.ops) != 1:
                    return None
                if not isinstance(test.ops[0], ast.Eq):
                    return None
                left, right = test.left, test.comparators[0]
                if (
                    isinstance(left, ast.Attribute)
                    and left.attr == "command"
                    and isinstance(right, ast.Constant)
                    and isinstance(right.value, str)
                ):
                    return right.value
                return None

            @staticmethod
            def _call_name(func: ast.AST) -> str:
                if isinstance(func, ast.Name):
                    return func.id
                if isinstance(func, ast.Attribute):
                    return func.attr
                return ""

        visitor = CommandVisitor()
        visitor.visit(tree)
        for command in ("start", "advance", "approve"):
            self.assertIn(command, visitor.command_calls)
            self.assertNotIn(
                "build_projection",
                visitor.command_calls[command],
                f"{command} must not consult the observability projection",
            )

    def test_start_succeeds_when_build_projection_would_raise(self) -> None:
        import agentflow.__main__ as main_mod

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "target"
            data_dir = temp_path / "home"
            init_repository(repository)
            commit_work_graph(
                repository,
                [
                    {
                        "id": "alpha",
                        "summary": "First",
                        "acceptance_criteria": ["a"],
                        "depends_on": [],
                    }
                ],
            )
            approve_graph(repository, data_dir)
            previous_cwd = Path.cwd()
            try:
                os.chdir(repository)
                with patch.object(
                    main_mod,
                    "build_projection",
                    side_effect=AssertionError(
                        "start must not consult the observability projection"
                    ),
                ):
                    with patch.object(
                        sys,
                        "argv",
                        [
                            "agentflow",
                            "start",
                            "--work-item",
                            "alpha",
                            "--data-dir",
                            str(data_dir),
                        ],
                    ):
                        with patch.object(sys, "stdout", new_callable=io.StringIO):
                            returncode = main_mod.main()
            finally:
                os.chdir(previous_cwd)
            self.assertEqual(returncode, 0)
            self.assertTrue((data_dir / "runs").is_dir())


class ProjectionCorruptionTests(unittest.TestCase):
    def test_invalid_json_preserves_prior_events_and_other_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "target"
            data_dir = temp_path / "home"
            init_repository(repository)
            save_work_graph([], repository)

            healthy = [
                {"run_id": "run-healthy", "sequence": 1, "type": "run_created"},
                {
                    "run_id": "run-healthy",
                    "sequence": 2,
                    "type": "workspace_ready",
                    "worktree": "/tmp/healthy",
                },
            ]
            write_events(
                data_dir / "runs" / "run-healthy",
                "".join(json.dumps(event) + "\n" for event in healthy),
            )
            (data_dir / "runs" / "run-healthy" / "task.json").write_text(
                json.dumps({"summary": "Healthy"}),
                encoding="utf-8",
            )

            good = {"run_id": "run-damaged", "sequence": 1, "type": "run_created"}
            write_events(
                data_dir / "runs" / "run-damaged",
                json.dumps(good) + "\n" + "{not-json\n",
            )
            (data_dir / "runs" / "run-damaged" / "task.json").write_text(
                json.dumps({"summary": "Damaged"}),
                encoding="utf-8",
            )

            projection = build_projection(
                data_dir=data_dir, repository=repository
            )
            run_ids = {entry["run_id"] for entry in projection["runs"]}
            self.assertEqual(run_ids, {"run-healthy", "run-damaged"})
            damaged = next(
                entry
                for entry in projection["evidence"]
                if entry["run_id"] == "run-damaged"
            )
            self.assertTrue(damaged["truncated"])
            self.assertEqual(damaged["events"], [good])
            healthy_evidence = next(
                entry
                for entry in projection["evidence"]
                if entry["run_id"] == "run-healthy"
            )
            self.assertFalse(healthy_evidence["truncated"])
            self.assertEqual(len(healthy_evidence["events"]), 2)

    def test_undecodable_bytes_preserve_prior_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "target"
            data_dir = temp_path / "home"
            init_repository(repository)
            save_work_graph([], repository)

            good = {"run_id": "run-bytes", "sequence": 1, "type": "run_created"}
            payload = (
                (json.dumps(good) + "\n").encode("utf-8")
                + b"\xff\xfe not utf-8\n"
                + (json.dumps({"sequence": 3, "type": "build_ready"}) + "\n").encode(
                    "utf-8"
                )
            )
            write_events(data_dir / "runs" / "run-bytes", payload)
            (data_dir / "runs" / "run-bytes" / "task.json").write_text(
                json.dumps({"summary": "Bytes"}),
                encoding="utf-8",
            )
            # Sibling healthy run must still appear.
            write_events(
                data_dir / "runs" / "run-ok",
                json.dumps(
                    {"run_id": "run-ok", "sequence": 1, "type": "run_created"}
                )
                + "\n",
            )

            events, truncated = read_events_tolerant(
                data_dir / "runs" / "run-bytes" / "events.jsonl"
            )
            self.assertTrue(truncated)
            self.assertEqual(events, [good])

            projection = build_projection(
                data_dir=data_dir, repository=repository
            )
            self.assertEqual(
                {entry["run_id"] for entry in projection["runs"]},
                {"run-bytes", "run-ok"},
            )
            damaged = next(
                entry
                for entry in projection["evidence"]
                if entry["run_id"] == "run-bytes"
            )
            self.assertTrue(damaged["truncated"])
            self.assertEqual(damaged["events"], [good])
            damaged_run = next(
                entry
                for entry in projection["runs"]
                if entry["run_id"] == "run-bytes"
            )
            self.assertTrue(damaged_run["evidence_truncated"])
            self.assertEqual(damaged_run["state"], "created")

    def test_undecodable_sibling_does_not_abort_work_projection(self) -> None:
        """Work completion must come from projected evidence, not list_runs.

        ``list_runs`` raises on undecodable bytes; the projection must still
        render work (items, ready, completed_ids) from healthy approved Runs.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "target"
            data_dir = temp_path / "home"
            init_repository(repository)
            save_work_graph(
                [
                    {
                        "id": "alpha",
                        "summary": "First",
                        "acceptance_criteria": ["a"],
                        "depends_on": [],
                    },
                    {
                        "id": "beta",
                        "summary": "Second",
                        "acceptance_criteria": ["b"],
                        "depends_on": ["alpha"],
                    },
                ],
                repository,
            )

            approved = data_dir / "runs" / "run-alpha"
            write_events(
                approved,
                "".join(
                    json.dumps(event) + "\n"
                    for event in (
                        {
                            "run_id": "run-alpha",
                            "sequence": 1,
                            "type": "run_created",
                        },
                        {
                            "run_id": "run-alpha",
                            "sequence": 2,
                            "type": "human_approved",
                            "approved_sha": "approved",
                        },
                    )
                ),
            )
            (approved / "task.json").write_text(
                json.dumps(
                    {
                        "summary": "First",
                        "source": {
                            "provider": "work-graph",
                            "work_item_id": "alpha",
                            "content_hash": "hash",
                            "captured_at": "2026-07-16T00:00:00+00:00",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (approved / "repository.json").write_text(
                json.dumps(
                    {"repository": str(repository), "base_sha": "base"}
                ),
                encoding="utf-8",
            )

            damaged = data_dir / "runs" / "run-damaged"
            write_events(
                damaged,
                (
                    json.dumps(
                        {
                            "run_id": "run-damaged",
                            "sequence": 1,
                            "type": "run_created",
                        }
                    )
                    + "\n"
                ).encode("utf-8")
                + b"\xff\xfe corrupt sibling\n",
            )
            (damaged / "task.json").write_text(
                json.dumps({"summary": "Damaged"}),
                encoding="utf-8",
            )

            with patch(
                "agentflow.run_kernel.list_runs",
                side_effect=AssertionError(
                    "projection must not derive work via list_runs"
                ),
            ), patch(
                "agentflow.work_graph.completed_work_item_ids",
                side_effect=AssertionError(
                    "projection must not call completed_work_item_ids"
                ),
            ):
                projection = build_projection(
                    data_dir=data_dir, repository=repository
                )

            self.assertEqual(
                {entry["run_id"] for entry in projection["runs"]},
                {"run-alpha", "run-damaged"},
            )
            self.assertEqual(projection["work"]["completed_ids"], ["alpha"])
            self.assertEqual(
                [item["id"] for item in projection["work"]["ready"]],
                ["beta"],
            )
            self.assertEqual(
                [item["id"] for item in projection["work"]["items"]],
                ["alpha", "beta"],
            )

    def test_events_after_corruption_do_not_affect_state_or_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repository = temp_path / "target"
            data_dir = temp_path / "home"
            init_repository(repository)
            save_work_graph(
                [
                    {
                        "id": "alpha",
                        "summary": "First",
                        "acceptance_criteria": ["a"],
                        "depends_on": [],
                    },
                    {
                        "id": "beta",
                        "summary": "Second",
                        "acceptance_criteria": ["b"],
                        "depends_on": ["alpha"],
                    },
                ],
                repository,
            )

            run_dir = data_dir / "runs" / "run-truncated"
            created = {
                "run_id": "run-truncated",
                "sequence": 1,
                "type": "run_created",
            }
            # Approval appears only after damage and must not count.
            payload = (
                (json.dumps(created) + "\n").encode("utf-8")
                + b"{not-json\n"
                + (
                    json.dumps(
                        {
                            "run_id": "run-truncated",
                            "sequence": 3,
                            "type": "human_approved",
                            "approved_sha": "should-not-apply",
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )
            write_events(run_dir, payload)
            (run_dir / "task.json").write_text(
                json.dumps(
                    {
                        "summary": "Truncated",
                        "source": {
                            "provider": "work-graph",
                            "work_item_id": "alpha",
                            "content_hash": "hash",
                            "captured_at": "2026-07-16T00:00:00+00:00",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "repository.json").write_text(
                json.dumps(
                    {"repository": str(repository), "base_sha": "base"}
                ),
                encoding="utf-8",
            )

            projection = build_projection(
                data_dir=data_dir, repository=repository
            )
            run_entry = projection["runs"][0]
            self.assertEqual(run_entry["state"], "created")
            self.assertNotIn("approved_sha", run_entry)
            self.assertTrue(run_entry["evidence_truncated"])
            self.assertEqual(projection["work"]["completed_ids"], [])
            self.assertEqual(
                [item["id"] for item in projection["work"]["ready"]],
                ["alpha"],
            )
            evidence = projection["evidence"][0]
            self.assertTrue(evidence["truncated"])
            self.assertEqual(evidence["events"], [created])

    def test_partial_trailing_line_is_growth_not_damage(self) -> None:
        """A last line without a newline is a concurrent append, not corruption."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            path = temp_path / "events.jsonl"
            good = {"run_id": "run-x", "sequence": 1, "type": "run_created"}
            partial = json.dumps(
                {"run_id": "run-x", "sequence": 2, "type": "build_ready"}
            )
            path.write_text(
                json.dumps(good) + "\n" + partial[: len(partial) // 2],
                encoding="utf-8",
            )
            events, truncated = read_events_tolerant(path)
            self.assertFalse(truncated)
            self.assertEqual(events, [good])

            # Once the writer finishes the line, it is read normally.
            path.write_text(
                json.dumps(good) + "\n" + partial + "\n",
                encoding="utf-8",
            )
            events, truncated = read_events_tolerant(path)
            self.assertFalse(truncated)
            self.assertEqual(len(events), 2)

    def test_non_object_json_line_stops_reading_like_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            path = temp_path / "events.jsonl"
            good = {"run_id": "run-x", "sequence": 1, "type": "run_created"}
            path.write_text(
                json.dumps(good)
                + "\n[1, 2]\n"
                + json.dumps(
                    {"run_id": "run-x", "sequence": 3, "type": "build_ready"}
                )
                + "\n",
                encoding="utf-8",
            )
            events, truncated = read_events_tolerant(path)
            self.assertTrue(truncated)
            self.assertEqual(events, [good])


if __name__ == "__main__":
    unittest.main()
