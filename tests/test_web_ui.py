from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from agentflow.projection import (  # noqa: E402
    build_projection,
    confined_file,
    confined_run_dir,
)
from agentflow.web_ui import (  # noqa: E402
    create_web_server,
    iter_run_stream,
)
from agentflow.work_graph import save_work_graph  # noqa: E402


def write_run(
    run_dir: Path,
    *,
    events: list[dict],
    summary: str = "A run",
    work_item_id: str | None = None,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    task: dict[str, object] = {"summary": summary}
    if work_item_id is not None:
        task["source"] = {
            "provider": "work-graph",
            "work_item_id": work_item_id,
            "content_hash": "hash",
            "captured_at": "2026-07-16T00:00:00+00:00",
        }
    (run_dir / "task.json").write_text(json.dumps(task), encoding="utf-8")
    (run_dir / "repository.json").write_text(
        json.dumps({"repository": "/target", "base_sha": "base"}),
        encoding="utf-8",
    )


def snapshot(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class ServedServer:
    """Start a bound web server on a daemon thread for the duration of a test."""

    def __init__(self, *, data_dir: Path, repository: Path) -> None:
        self.server = create_web_server(
            data_dir=data_dir, repository=repository, host="127.0.0.1", port=0
        )
        host, port = self.server.server_address[0], self.server.server_address[1]
        self.base_url = f"http://{host}:{port}"
        self._thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )

    def __enter__(self) -> "ServedServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)

    def get(self, path: str) -> tuple[int, bytes]:
        try:
            with urlopen(self.base_url + path, timeout=5) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()

    def request(self, method: str, path: str) -> int:
        request = Request(self.base_url + path, method=method)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status
        except HTTPError as error:
            return error.code


def parse_sse(body: bytes) -> list[dict[str, str]]:
    frames: list[dict[str, str]] = []
    for block in body.decode("utf-8").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        for line in block.splitlines():
            if line.startswith("data:"):
                frames.append(json.loads(line[len("data:"):].strip()))
    return frames


class WebProjectionTests(unittest.TestCase):
    def test_api_projection_matches_build_projection_and_is_read_only(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir = temp_path / "home"
            repository = temp_path / "target"
            repository.mkdir()
            save_work_graph(
                [
                    {
                        "id": "alpha",
                        "summary": "First",
                        "acceptance_criteria": ["a"],
                        "depends_on": [],
                    }
                ],
                repository,
            )
            write_run(
                data_dir / "runs" / "run-a",
                events=[
                    {"run_id": "run-a", "sequence": 1, "type": "run_created"},
                    {
                        "run_id": "run-a",
                        "sequence": 2,
                        "type": "workspace_ready",
                        "worktree": "/tmp/run-a",
                    },
                ],
                work_item_id="alpha",
            )
            expected = build_projection(data_dir=data_dir, repository=repository)
            before = snapshot(temp_path)
            with ServedServer(data_dir=data_dir, repository=repository) as served:
                status, body = served.get("/api/projection")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), expected)
            self.assertEqual(snapshot(temp_path), before)

    def test_index_page_is_served_and_has_no_mutation_controls(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir = temp_path / "home"
            repository = temp_path / "target"
            repository.mkdir()
            save_work_graph([], repository)
            (data_dir / "runs").mkdir(parents=True)
            with ServedServer(data_dir=data_dir, repository=repository) as served:
                status, body = served.get("/")
            self.assertEqual(status, 200)
            page = body.decode("utf-8").lower()
            self.assertIn("read-only", page)
            # No interactive mutation controls: no forms, buttons, inputs, or
            # non-GET fetches. The page only reads the projection and streams.
            for forbidden in (
                "<form",
                "<button",
                "<input",
                'method="post"',
                "method: \"post\"",
                "method: 'post'",
            ):
                self.assertNotIn(forbidden, page, forbidden)

    def test_mutation_methods_are_refused_and_write_nothing(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir = temp_path / "home"
            repository = temp_path / "target"
            repository.mkdir()
            save_work_graph([], repository)
            write_run(
                data_dir / "runs" / "run-a",
                events=[
                    {"run_id": "run-a", "sequence": 1, "type": "run_created"}
                ],
            )
            before = snapshot(temp_path)
            with ServedServer(data_dir=data_dir, repository=repository) as served:
                for method in ("POST", "PUT", "DELETE", "PATCH"):
                    for path in (
                        "/api/projection",
                        "/api/runs/run-a/approve",
                        "/approve",
                        "/start",
                    ):
                        self.assertEqual(
                            served.request(method, path),
                            405,
                            f"{method} {path}",
                        )
                # An unknown GET route is a plain 404, not a mutation surface.
                self.assertEqual(served.get("/api/start")[0], 404)
            self.assertEqual(snapshot(temp_path), before)


class WebStreamTests(unittest.TestCase):
    def test_stream_emits_events_and_transcripts_over_http(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir = temp_path / "home"
            repository = temp_path / "target"
            repository.mkdir()
            save_work_graph([], repository)
            run_dir = data_dir / "runs" / "run-a"
            write_run(
                run_dir,
                events=[
                    {"run_id": "run-a", "sequence": 1, "type": "run_created"}
                ],
            )
            (run_dir / "builder-1-transcript.jsonl").write_text(
                json.dumps({"role": "builder", "text": "hello"}) + "\n"
                + json.dumps({"role": "builder", "text": "world"}) + "\n",
                encoding="utf-8",
            )
            with ServedServer(data_dir=data_dir, repository=repository) as served:
                status, body = served.get("/api/runs/run-a/stream?follow=0")
            self.assertEqual(status, 200)
            frames = parse_sse(body)
            kinds = {(frame["kind"], frame["source"]) for frame in frames}
            self.assertIn(("event", "events.jsonl"), kinds)
            self.assertIn(("transcript", "builder-1-transcript.jsonl"), kinds)
            transcript_lines = [
                json.loads(frame["line"])["text"]
                for frame in frames
                if frame["kind"] == "transcript"
            ]
            self.assertEqual(transcript_lines, ["hello", "world"])

    def test_iter_run_stream_only_emits_complete_lines(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run-a"
            run_dir.mkdir()
            (run_dir / "events.jsonl").write_text("", encoding="utf-8")
            transcript = run_dir / "reviewer-1-transcript.jsonl"
            # A complete line followed by a partial (no trailing newline) line.
            transcript.write_text(
                json.dumps({"text": "done"}) + "\n" + '{"text": "partial"',
                encoding="utf-8",
            )
            frames = list(iter_run_stream(run_dir, follow=False))
            lines = [json.loads(frame_line(frame))["text"] for frame in frames]
            self.assertEqual(lines, ["done"])

    def test_iter_run_stream_follows_lines_appended_between_polls(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run-a"
            run_dir.mkdir()
            (run_dir / "events.jsonl").write_text("", encoding="utf-8")
            transcript = run_dir / "builder-1-transcript.jsonl"
            transcript.write_text(
                json.dumps({"text": "first"}) + "\n", encoding="utf-8"
            )
            state = {"polls": 0}

            def fake_sleep(_seconds: float) -> None:
                if state["polls"] == 0:
                    with transcript.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps({"text": "second"}) + "\n")
                state["polls"] += 1

            def should_continue() -> bool:
                return state["polls"] < 2

            frames = list(
                iter_run_stream(
                    run_dir,
                    follow=True,
                    sleep=fake_sleep,
                    should_continue=should_continue,
                )
            )
            lines = [
                json.loads(frame_line(frame))["text"]
                for frame in frames
                if sse_source(frame) == "builder-1-transcript.jsonl"
            ]
            self.assertEqual(lines, ["first", "second"])

    def test_server_stops_serving_after_shutdown(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir = temp_path / "home"
            repository = temp_path / "target"
            repository.mkdir()
            save_work_graph([], repository)
            (data_dir / "runs").mkdir(parents=True)
            server = create_web_server(
                data_dir=data_dir, repository=repository, port=0
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.assertTrue(server.is_serving())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            self.assertFalse(server.is_serving())

    def test_live_http_stream_tails_lines_appended_after_connect(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir = temp_path / "home"
            repository = temp_path / "target"
            repository.mkdir()
            save_work_graph([], repository)
            run_dir = data_dir / "runs" / "run-a"
            write_run(
                run_dir,
                events=[
                    {"run_id": "run-a", "sequence": 1, "type": "run_created"}
                ],
            )
            transcript = run_dir / "builder-1-transcript.jsonl"
            transcript.write_text(
                json.dumps({"text": "first"}) + "\n", encoding="utf-8"
            )
            with ServedServer(data_dir=data_dir, repository=repository) as served:
                response = urlopen(
                    served.base_url + "/api/runs/run-a/stream", timeout=8
                )
                chunks: list[bytes] = []

                def reader() -> None:
                    try:
                        while True:
                            chunk = response.read(64)
                            if not chunk:
                                return
                            chunks.append(chunk)
                    except Exception:
                        return

                thread = threading.Thread(target=reader, daemon=True)
                thread.start()
                try:
                    # Append after the connection is live; the tail must pick it
                    # up on a later poll without reconnecting.
                    time.sleep(0.3)
                    with transcript.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps({"text": "second"}) + "\n")

                    def seen_both() -> bool:
                        data = b"".join(chunks).decode("utf-8", "ignore")
                        return "first" in data and "second" in data

                    deadline = time.time() + 6
                    while time.time() < deadline and not seen_both():
                        time.sleep(0.1)
                    self.assertTrue(seen_both(), b"".join(chunks))
                finally:
                    response.close()
                    thread.join(timeout=5)

    def test_stream_404_for_missing_run(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir = temp_path / "home"
            repository = temp_path / "target"
            repository.mkdir()
            save_work_graph([], repository)
            (data_dir / "runs").mkdir(parents=True)
            with ServedServer(data_dir=data_dir, repository=repository) as served:
                self.assertEqual(
                    served.get("/api/runs/nope/stream?follow=0")[0], 404
                )


def frame_line(frame: str) -> str:
    for line in frame.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())["line"]
    raise AssertionError(f"no data line in frame {frame!r}")


class ConfinementTests(unittest.TestCase):
    def test_dot_and_dotdot_and_separators_are_rejected(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            runs_dir = Path(temp_dir) / "runs"
            (runs_dir / "run-a").mkdir(parents=True)
            self.assertIsNotNone(confined_run_dir(runs_dir, "run-a"))
            for bad in (".", "..", "", "a/b", "../run-a", "run-a/x"):
                self.assertIsNone(
                    confined_run_dir(runs_dir, bad), f"{bad!r} must be rejected"
                )

    def test_encoded_dotdot_run_id_is_a_404_not_traversal(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir = temp_path / "home"
            repository = temp_path / "target"
            repository.mkdir()
            save_work_graph([], repository)
            (data_dir / "runs").mkdir(parents=True)
            with ServedServer(data_dir=data_dir, repository=repository) as served:
                self.assertEqual(
                    served.get("/api/runs/%2e%2e/stream?follow=0")[0], 404
                )

    def test_symlinked_run_dir_escaping_home_is_omitted(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir = temp_path / "home"
            repository = temp_path / "target"
            repository.mkdir()
            save_work_graph([], repository)
            write_run(
                data_dir / "runs" / "run-ok",
                events=[
                    {"run_id": "run-ok", "sequence": 1, "type": "run_created"}
                ],
            )
            # A fully-formed run directory OUTSIDE runs/, reachable only via a
            # symlink placed inside runs/. Confinement must omit it.
            outside = data_dir / "outside-run"
            write_run(
                outside,
                events=[
                    {"run_id": "escape", "sequence": 1, "type": "run_created"}
                ],
            )
            os.symlink(outside, data_dir / "runs" / "escape")

            projection = build_projection(
                data_dir=data_dir, repository=repository
            )
            run_ids = {entry["run_id"] for entry in projection["runs"]}
            self.assertEqual(run_ids, {"run-ok"})
            self.assertIsNone(
                confined_run_dir(data_dir / "runs", "escape")
            )

    def test_evidence_symlink_escaping_run_dir_is_refused(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir = temp_path / "home"
            repository = temp_path / "target"
            repository.mkdir()
            save_work_graph([], repository)
            write_run(
                data_dir / "runs" / "run-ok",
                events=[
                    {"run_id": "run-ok", "sequence": 1, "type": "run_created"}
                ],
            )
            # run-x's events.jsonl is a symlink to a file outside its run dir.
            run_x = data_dir / "runs" / "run-x"
            run_x.mkdir(parents=True)
            secret = data_dir / "secret.jsonl"
            secret.write_text(
                json.dumps(
                    {"run_id": "run-x", "sequence": 1, "type": "human_approved"}
                )
                + "\n",
                encoding="utf-8",
            )
            os.symlink(secret, run_x / "events.jsonl")

            self.assertIsNone(confined_file(run_x, "events.jsonl"))
            projection = build_projection(
                data_dir=data_dir, repository=repository
            )
            run_ids = {entry["run_id"] for entry in projection["runs"]}
            self.assertEqual(run_ids, {"run-ok"})

    def test_transcript_symlink_escaping_run_dir_is_refused(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            run_dir = temp_path / "runs" / "run-a"
            write_run(
                run_dir,
                events=[
                    {"run_id": "run-a", "sequence": 1, "type": "run_created"}
                ],
            )
            (run_dir / "builder-1-transcript.jsonl").write_text(
                json.dumps({"text": "in-bounds"}) + "\n", encoding="utf-8"
            )
            outside = temp_path / "outside-transcript.jsonl"
            outside.write_text(
                json.dumps({"text": "secret"}) + "\n", encoding="utf-8"
            )
            os.symlink(outside, run_dir / "reviewer-1-transcript.jsonl")

            frames = list(iter_run_stream(run_dir, follow=False))
            sources = {sse_source(frame) for frame in frames}
            self.assertIn("builder-1-transcript.jsonl", sources)
            self.assertNotIn("reviewer-1-transcript.jsonl", sources)

    def test_circular_evidence_symlink_is_skipped_not_raised(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir = temp_path / "home"
            repository = temp_path / "target"
            repository.mkdir()
            save_work_graph([], repository)
            write_run(
                data_dir / "runs" / "run-ok",
                events=[
                    {"run_id": "run-ok", "sequence": 1, "type": "run_created"}
                ],
            )
            loop = data_dir / "runs" / "run-loop"
            loop.mkdir(parents=True)
            os.symlink(loop / "events.jsonl", loop / "events.jsonl")

            # Must not raise; the sibling in-bounds run still projects.
            projection = build_projection(
                data_dir=data_dir, repository=repository
            )
            run_ids = {entry["run_id"] for entry in projection["runs"]}
            self.assertEqual(run_ids, {"run-ok"})

    def test_circular_transcript_symlink_is_skipped_not_raised(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir = temp_path / "home"
            repository = temp_path / "target"
            repository.mkdir()
            save_work_graph([], repository)
            run_dir = data_dir / "runs" / "run-a"
            write_run(
                run_dir,
                events=[
                    {"run_id": "run-a", "sequence": 1, "type": "run_created"}
                ],
            )
            (run_dir / "builder-1-transcript.jsonl").write_text(
                json.dumps({"text": "ok"}) + "\n", encoding="utf-8"
            )
            circular = run_dir / "reviewer-1-transcript.jsonl"
            os.symlink(circular, circular)

            # Streaming must not raise; events and the in-bounds transcript
            # still stream, the circular transcript is skipped.
            frames = list(iter_run_stream(run_dir, follow=False))
            sources = {sse_source(frame) for frame in frames}
            self.assertIn("events.jsonl", sources)
            self.assertIn("builder-1-transcript.jsonl", sources)
            self.assertNotIn("reviewer-1-transcript.jsonl", sources)
            # /api/projection also keeps serving this run.
            projection = build_projection(
                data_dir=data_dir, repository=repository
            )
            self.assertEqual(
                {entry["run_id"] for entry in projection["runs"]}, {"run-a"}
            )


class ServeCommandTests(unittest.TestCase):
    def test_serve_reports_url_and_serves_projection(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir = temp_path / "home"
            repository = temp_path / "target"
            repository.mkdir()
            save_work_graph([], repository)
            write_run(
                data_dir / "runs" / "run-a",
                events=[
                    {"run_id": "run-a", "sequence": 1, "type": "run_created"}
                ],
            )
            with subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "agentflow",
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--data-dir",
                    str(data_dir),
                    "--repository",
                    str(repository),
                ],
                cwd=repository,
                env={**os.environ, "PYTHONPATH": str(SRC_ROOT)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ) as proc:
                try:
                    line = proc.stdout.readline()
                    info = json.loads(line)
                    self.assertEqual(info["state"], "serving")
                    with urlopen(
                        info["url"] + "/api/projection", timeout=5
                    ) as resp:
                        body = json.loads(resp.read())
                    self.assertEqual(
                        [entry["run_id"] for entry in body["runs"]], ["run-a"]
                    )
                finally:
                    proc.terminate()
                    proc.wait(timeout=5)


def sse_source(frame: str) -> str:
    for line in frame.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())["source"]
    raise AssertionError(f"no data line in frame {frame!r}")


if __name__ == "__main__":
    unittest.main()
