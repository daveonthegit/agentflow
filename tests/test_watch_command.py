from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from test_advance_command import (
    PROJECT_ROOT,
    advance_tester,
    agentflow,
    create_profiled_run,
    create_verified_run,
)


PLANNER_STREAM_STUB = """#!/usr/bin/env python3
import json
import sys

arguments = sys.argv[1:]


def value(flag):
    return arguments[arguments.index(flag) + 1]


assert value("--output-format") == "stream-json"
assert "--include-partial-messages" not in arguments
assert "planner" in sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({
    "type": "assistant",
    "message": {"content": "planning the health endpoint"},
}))
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "planned",
    "structured_output": {
        "files_to_modify": ["README.md"],
        "risks": [],
        "steps": [{
            "description": "Document the health endpoint",
            "id": "P1",
            "verification": "The authoritative checks pass"
        }],
        "summary": "Add a health endpoint"
    }
}))
"""

BUILDER_STREAM_STUB = """#!/usr/bin/env python3
import json
from pathlib import Path
import sys

arguments = sys.argv[1:]


def value(flag):
    return arguments[arguments.index(flag) + 1]


assert value("--output-format") == "stream-json"
assert "builder" in sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({
    "type": "assistant",
    "message": {"content": "editing the README"},
}))
Path("README.md").write_text(
    "# Target\\n\\nHealth endpoint documented.\\n", encoding="utf-8"
)
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "built",
    "structured_output": {
        "commands_run": [],
        "files_changed": ["README.md"],
        "steps_completed": ["P1"],
        "unresolved_issues": []
    }
}))
"""

REVIEWER_STREAM_STUB = """#!/usr/bin/env python3
import json
import sys

arguments = sys.argv[1:]


def value(flag):
    return arguments[arguments.index(flag) + 1]


assert value("--output-format") == "stream-json"
assert "reviewer" in sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({
    "type": "assistant",
    "message": {"content": "reviewing the candidate"},
}))
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "reviewed",
    "structured_output": {
        "disposition": "approve",
        "findings": []
    }
}))
"""


def base_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AGENTFLOW_CLAUDE")
    }
    environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    return environment


def write_stub(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def read_events(data_dir: Path, run_id: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (data_dir / "runs" / run_id / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


class WatchCommandTests(unittest.TestCase):
    def test_claude_stages_write_referenced_transcripts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = base_environment()
            fake_claude = temp_path / "claude"
            write_stub(fake_claude, PLANNER_STREAM_STUB)
            environment["AGENTFLOW_CLAUDE"] = str(fake_claude)
            _, data_dir, run_id = create_profiled_run(temp_path, environment)
            run_dir = data_dir / "runs" / run_id

            planned = agentflow(
                "advance",
                run_id,
                "--adapter",
                "claude",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertEqual(planned.returncode, 0, planned.stderr)
            self.assertEqual(json.loads(planned.stdout)["state"], "planned")
            planner_transcript = run_dir / "planner-transcript.jsonl"
            self.assertTrue(planner_transcript.is_file())
            transcript_lines = [
                json.loads(line)
                for line in planner_transcript.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertIn(
                "planning the health endpoint",
                planner_transcript.read_text(encoding="utf-8"),
            )
            self.assertEqual(transcript_lines[-1]["type"], "result")
            # Structured output is still parsed from the stream's result event.
            plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["files_to_modify"], ["README.md"])
            events = read_events(data_dir, run_id)
            plan_ready = next(e for e in events if e["type"] == "plan_ready")
            self.assertEqual(
                Path(plan_ready["transcript"]).resolve(),
                planner_transcript.resolve(),
            )

            write_stub(fake_claude, BUILDER_STREAM_STUB)
            built = agentflow(
                "advance",
                run_id,
                "--adapter",
                "claude",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertEqual(built.returncode, 0, built.stderr)
            self.assertEqual(json.loads(built.stdout)["state"], "built")
            builder_transcript = run_dir / "builder-1-transcript.jsonl"
            self.assertTrue(builder_transcript.is_file())
            self.assertIn(
                "editing the README",
                builder_transcript.read_text(encoding="utf-8"),
            )
            events = read_events(data_dir, run_id)
            build_ready = next(e for e in events if e["type"] == "build_ready")
            self.assertEqual(
                Path(build_ready["transcript"]).resolve(),
                builder_transcript.resolve(),
            )

    def test_watch_prints_events_and_transcript_and_exits_at_blocking_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = base_environment()
            data_dir, run_id = create_verified_run(temp_path, environment)
            advance_tester(temp_path, data_dir, run_id, environment)
            run_dir = data_dir / "runs" / run_id

            fake_claude = temp_path / "claude"
            write_stub(fake_claude, REVIEWER_STREAM_STUB)
            claude_environment = {**environment, "AGENTFLOW_CLAUDE": str(fake_claude)}
            reviewed = agentflow(
                "advance",
                run_id,
                "--adapter",
                "claude",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=claude_environment,
            )
            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
            self.assertEqual(json.loads(reviewed.stdout)["state"], "awaiting_human")
            reviewer_transcript = run_dir / "reviewer-1-transcript.jsonl"
            self.assertTrue(reviewer_transcript.is_file())

            watched = agentflow(
                "watch",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertEqual(watched.returncode, 0, watched.stderr)
            # Prints new event lines from the Run's event log.
            self.assertIn('"type": "awaiting_human"', watched.stdout)
            self.assertIn('"type": "review_ready"', watched.stdout)
            # Prints lines from the growing role transcript.
            self.assertIn("reviewing the candidate", watched.stdout)
            # Ends with a final status line at the blocking state.
            self.assertIn(f"run {run_id} awaiting_human", watched.stdout)

    def test_watch_is_read_only_and_creates_no_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = base_environment()
            data_dir, run_id = create_verified_run(temp_path, environment)
            advance_tester(temp_path, data_dir, run_id, environment)
            run_dir = data_dir / "runs" / run_id

            fake_claude = temp_path / "claude"
            write_stub(fake_claude, REVIEWER_STREAM_STUB)
            claude_environment = {**environment, "AGENTFLOW_CLAUDE": str(fake_claude)}
            reviewed = agentflow(
                "advance",
                run_id,
                "--adapter",
                "claude",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=claude_environment,
            )
            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)

            before = sorted(p.name for p in run_dir.iterdir())
            events_before = (run_dir / "events.jsonl").read_text(encoding="utf-8")

            watched = agentflow(
                "watch",
                run_id,
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(watched.returncode, 0, watched.stderr)

            after = sorted(p.name for p in run_dir.iterdir())
            events_after = (run_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertEqual(before, after)
            self.assertEqual(events_before, events_after)


if __name__ == "__main__":
    unittest.main()
