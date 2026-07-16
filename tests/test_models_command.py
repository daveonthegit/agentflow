from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from test_advance_command import (
    PROJECT_ROOT,
    agentflow,
    create_profiled_run,
    create_tested_run,
)


SUGGESTED = {
    "builder": "opus",
    "planner": "opus",
    "reviewer": "opus",
    "tester": "opus",
}
CURSOR_SUGGESTED = {
    "builder": "claude-opus-4-8-thinking-high",
    "planner": "claude-opus-4-8-thinking-high",
    "reviewer": "claude-opus-4-8-thinking-high",
    "tester": "claude-opus-4-8-thinking-high",
}

PLANNER_STUB_TEMPLATE = """#!/usr/bin/env python3
import json
import sys

arguments = sys.argv[1:]


def value(flag):
    return arguments[arguments.index(flag) + 1]


assert value("--model") == {expected_model!r}, value("--model")
assert "planner" in sys.stdin.read()
print(json.dumps({{
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "planned",
    "structured_output": {{
        "files_to_modify": ["README.md"],
        "risks": [],
        "steps": [{{
            "description": "Document the health endpoint",
            "id": "P1",
            "verification": "The authoritative checks pass"
        }}],
        "summary": "Add a health endpoint"
    }}
}}))
"""

BUILDER_STUB_TEMPLATE = """#!/usr/bin/env python3
import json
from pathlib import Path
import sys

arguments = sys.argv[1:]


def value(flag):
    return arguments[arguments.index(flag) + 1]


assert value("--model") == {expected_model!r}, value("--model")
assert "builder" in sys.stdin.read()
Path("README.md").write_text(
    "# Target\\n\\nHealth endpoint documented.\\n", encoding="utf-8"
)
print(json.dumps({{
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "built",
    "structured_output": {{
        "commands_run": [],
        "files_changed": ["README.md"],
        "steps_completed": ["P1"],
        "unresolved_issues": []
    }}
}}))
"""


RECORDING_PLANNER_STUB = """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]


def value(flag):
    return arguments[arguments.index(flag) + 1]


with Path(os.environ["AGENTFLOW_STUB_MODEL_LOG"]).open(
    "a", encoding="utf-8"
) as handle:
    handle.write(value("--model") + "\\n")
assert "planner" in sys.stdin.read()
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


def base_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AGENTFLOW_CLAUDE")
    }
    environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    return environment


def write_stub(path: Path, template: str, expected_model: str) -> None:
    path.write_text(
        template.format(expected_model=expected_model),
        encoding="utf-8",
    )
    path.chmod(0o755)


def read_events(data_dir: Path, run_id: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (data_dir / "runs" / run_id / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


class ModelsCommandTests(unittest.TestCase):
    def test_planner_falls_back_to_the_suggested_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = base_environment()
            fake_claude = temp_path / "claude"
            write_stub(fake_claude, PLANNER_STUB_TEMPLATE, "opus")
            environment["AGENTFLOW_CLAUDE"] = str(fake_claude)
            _, data_dir, run_id = create_profiled_run(temp_path, environment)

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

    def test_planner_uses_the_recorded_model_routing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = base_environment()
            fake_claude = temp_path / "claude"
            write_stub(fake_claude, PLANNER_STUB_TEMPLATE, "recorded-model")
            environment["AGENTFLOW_CLAUDE"] = str(fake_claude)
            _, data_dir, run_id = create_profiled_run(temp_path, environment)
            recorded = agentflow(
                "models",
                "--adapter",
                "claude",
                "--set",
                "planner=recorded-model",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(recorded.returncode, 0, recorded.stderr)

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

    def test_environment_variable_overrides_the_recorded_routing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = base_environment()
            fake_claude = temp_path / "claude"
            write_stub(fake_claude, PLANNER_STUB_TEMPLATE, "environment-model")
            environment["AGENTFLOW_CLAUDE"] = str(fake_claude)
            environment["AGENTFLOW_CLAUDE_PLANNER_MODEL"] = "environment-model"
            _, data_dir, run_id = create_profiled_run(temp_path, environment)
            recorded = agentflow(
                "models",
                "--adapter",
                "claude",
                "--set",
                "planner=recorded-model",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(recorded.returncode, 0, recorded.stderr)

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

    def test_advance_model_option_overrides_the_environment_variable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = base_environment()
            fake_claude = temp_path / "claude"
            write_stub(fake_claude, PLANNER_STUB_TEMPLATE, "explicit-model")
            environment["AGENTFLOW_CLAUDE"] = str(fake_claude)
            environment["AGENTFLOW_CLAUDE_PLANNER_MODEL"] = "environment-model"
            _, data_dir, run_id = create_profiled_run(temp_path, environment)

            planned = agentflow(
                "advance",
                run_id,
                "--adapter",
                "claude",
                "--model",
                "explicit-model",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertEqual(planned.returncode, 0, planned.stderr)
            self.assertEqual(json.loads(planned.stdout)["state"], "planned")

    def test_advance_rejects_model_option_for_non_routing_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = base_environment()

            rejected = agentflow(
                "advance",
                "some-run",
                "--adapter",
                "fake",
                "--model",
                "opus",
                "--data-dir",
                str(temp_path / "agentflow-home"),
                cwd=temp_path,
                environment=environment,
            )

            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn(
                "--model requires --adapter claude or cursor", rejected.stderr
            )

    def test_models_get_and_set_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = base_environment()
            data_dir = temp_path / "agentflow-home"

            empty = agentflow(
                "models",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(empty.returncode, 0, empty.stderr)
            self.assertEqual(
                json.loads(empty.stdout),
                {
                    "claude": {"recorded": {}, "suggested": SUGGESTED},
                    "cursor": {"recorded": {}, "suggested": CURSOR_SUGGESTED},
                },
            )

            recorded = agentflow(
                "models",
                "--adapter",
                "claude",
                "--set",
                "planner=fable",
                "--set",
                "builder=opus",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(recorded.returncode, 0, recorded.stderr)
            self.assertEqual(
                json.loads(recorded.stdout),
                {
                    "claude": {
                        "recorded": {"builder": "opus", "planner": "fable"},
                        "suggested": SUGGESTED,
                    },
                    "cursor": {"recorded": {}, "suggested": CURSOR_SUGGESTED},
                },
            )
            self.assertEqual(
                json.loads((data_dir / "models.json").read_text(encoding="utf-8")),
                {"claude": {"builder": "opus", "planner": "fable"}},
            )

            replayed = agentflow(
                "models",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(replayed.returncode, 0, replayed.stderr)
            self.assertEqual(json.loads(replayed.stdout), json.loads(recorded.stdout))

    def test_models_set_rejects_an_unknown_role_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = base_environment()
            data_dir = temp_path / "agentflow-home"

            rejected = agentflow(
                "models",
                "--adapter",
                "claude",
                "--set",
                "merger=opus",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("merger", rejected.stderr)
            self.assertFalse((data_dir / "models.json").exists())

    def test_models_set_accepts_the_tester_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = base_environment()
            data_dir = temp_path / "agentflow-home"

            recorded = agentflow(
                "models",
                "--adapter",
                "cursor",
                "--set",
                "tester=custom-tester-model",
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )

            self.assertEqual(recorded.returncode, 0, recorded.stderr)
            self.assertEqual(
                json.loads(recorded.stdout)["cursor"]["recorded"],
                {"tester": "custom-tester-model"},
            )

    def test_stage_events_record_the_resolved_model_for_claude(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = base_environment()
            fake_claude = temp_path / "claude"
            write_stub(fake_claude, PLANNER_STUB_TEMPLATE, "opus")
            environment["AGENTFLOW_CLAUDE"] = str(fake_claude)
            _, data_dir, run_id = create_profiled_run(temp_path, environment)
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
            write_stub(fake_claude, BUILDER_STUB_TEMPLATE, "opus")

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
            events = read_events(data_dir, run_id)
            plan_ready = next(
                event for event in events if event["type"] == "plan_ready"
            )
            build_ready = next(
                event for event in events if event["type"] == "build_ready"
            )
            self.assertEqual(plan_ready["model"], "opus")
            self.assertEqual(build_ready["model"], "opus")

    def test_stage_event_model_matches_the_single_cli_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = base_environment()
            fake_claude = temp_path / "claude"
            fake_claude.write_text(RECORDING_PLANNER_STUB, encoding="utf-8")
            fake_claude.chmod(0o755)
            model_log = temp_path / "model-argv.log"
            environment["AGENTFLOW_CLAUDE"] = str(fake_claude)
            environment["AGENTFLOW_STUB_MODEL_LOG"] = str(model_log)
            _, data_dir, run_id = create_profiled_run(temp_path, environment)

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
            recorded = model_log.read_text(encoding="utf-8").splitlines()
            # The CLI is the only consumer of the resolved model; it is invoked
            # exactly once, proving the model is resolved a single time.
            self.assertEqual(len(recorded), 1, recorded)
            events = read_events(data_dir, run_id)
            plan_ready = next(
                event for event in events if event["type"] == "plan_ready"
            )
            # The event's provenance is stamped from that same single
            # resolution, never re-resolved.
            self.assertEqual(plan_ready["model"], recorded[0])

    def test_fake_adapter_stage_events_record_no_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environment = base_environment()
            data_dir, run_id = create_tested_run(temp_path, environment)
            fixture_path = temp_path / "adapter-fixture.json"
            fixture_path.write_text(
                json.dumps({"reviewer": {"disposition": "approve", "findings": []}}),
                encoding="utf-8",
            )
            reviewed = agentflow(
                "advance",
                run_id,
                "--adapter",
                "fake",
                "--adapter-fixture",
                str(fixture_path),
                "--data-dir",
                str(data_dir),
                cwd=temp_path,
                environment=environment,
            )
            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)

            events = read_events(data_dir, run_id)

            stage_events = {event["type"] for event in events}
            self.assertLessEqual(
                {
                    "plan_ready",
                    "build_ready",
                    "checks_passed",
                    "tests_ready",
                    "review_ready",
                },
                stage_events,
            )
            for event in events:
                self.assertNotIn("model", event, event)


if __name__ == "__main__":
    unittest.main()
