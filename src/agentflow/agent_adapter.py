from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from typing import Any, Protocol, TextIO

from .contracts import (
    ContractError,
    contract_schema,
    validate_builder_report,
    validate_plan,
    validate_review,
    validate_tester_report,
)


ROLE_INSTRUCTIONS = {
    "planner": (
        "Analyze the task and repository. Do not edit files. Produce the "
        "smallest viable plan and only the required structured output."
    ),
    "builder": (
        "Implement the approved plan in this Workspace. Do not merge or "
        "push. Modify only planned files, then return the required report."
    ),
    "reviewer": (
        "Review the candidate against the task, plan, and checks. Do not "
        "edit files. Return only structured findings and disposition."
    ),
    "tester": (
        "Adversarially probe the candidate against the Task Spec and "
        "acceptance criteria by strengthening tests only. Modify only files "
        "under the declared test paths; never touch production code. Express "
        "any suspected defect as a failing test under those test paths. Prose "
        "findings alone do not block the workflow — only tests the "
        "authoritative checks run can. Return only the required report."
    ),
}

ROLES = ("planner", "builder", "reviewer", "tester")

SUGGESTED_MODELS = {
    "claude": {
        "builder": "opus",
        "planner": "opus",
        "reviewer": "opus",
        "tester": "opus",
    },
    "cursor": {
        "builder": "claude-opus-4-8-thinking-high",
        "planner": "claude-opus-4-8-thinking-high",
        "reviewer": "claude-opus-4-8-thinking-high",
        "tester": "claude-opus-4-8-thinking-high",
    },
}


def _validate_role(role: str) -> None:
    if role not in ROLES:
        raise ValueError(
            f"unknown role {role}; expected one of {', '.join(ROLES)}"
        )


def read_model_routing(data_dir: Path) -> dict[str, dict[str, str]]:
    routing_path = data_dir / "models.json"
    if not routing_path.exists():
        return {}
    return json.loads(routing_path.read_text(encoding="utf-8"))


def record_model_routing(
    data_dir: Path,
    adapter_name: str,
    updates: dict[str, str],
) -> dict[str, dict[str, str]]:
    for role in updates:
        _validate_role(role)
    routing = read_model_routing(data_dir)
    adapter_routing = dict(routing.get(adapter_name, {}))
    adapter_routing.update(updates)
    routing[adapter_name] = adapter_routing
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "models.json").write_text(
        json.dumps(routing, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return routing


def role_prompt(role: str, request: dict[str, Any]) -> str:
    return (
        f"You are the Agentflow {role} Agent Role.\n\n"
        f"{ROLE_INSTRUCTIONS[role]}\n\n"
        "Workflow context:\n"
        + json.dumps(request, indent=2, sort_keys=True)
    )


def _validate_role_output(role: str, value: Any) -> dict[str, Any]:
    validators = {
        "planner": validate_plan,
        "builder": validate_builder_report,
        "reviewer": validate_review,
        "tester": validate_tester_report,
    }
    return validators[role](value)


def _claude_result_diagnostics(result_event: dict[str, Any]) -> str:
    """Format available Claude result fields for failure messages."""
    diagnostics = {
        key: result_event.get(key)
        for key in ("subtype", "num_turns", "total_cost_usd")
        if result_event.get(key) is not None
    }
    if not diagnostics:
        return ""
    return f" {json.dumps(diagnostics, sort_keys=True)}"


def _parse_cursor_output(role: str, raw_output: Any) -> dict[str, Any]:
    if isinstance(raw_output, dict):
        return _validate_role_output(role, raw_output)
    if not isinstance(raw_output, str):
        raise ContractError("Cursor result must contain text or an object")
    decoder = json.JSONDecoder()
    errors: list[Exception] = []
    try:
        return _validate_role_output(role, json.loads(raw_output))
    except (ContractError, json.JSONDecodeError, TypeError) as error:
        errors.append(error)
    # Cursor may concatenate progress prose with the final answer in the result
    # envelope. Scan every object boundary and accept only an object that passes
    # the role contract; arbitrary prose or unrelated JSON remains untrusted.
    for index, character in enumerate(raw_output):
        if character != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(raw_output[index:])
            return _validate_role_output(role, candidate)
        except (ContractError, json.JSONDecodeError, TypeError) as error:
            errors.append(error)
    raise ContractError(f"Cursor result contains no valid role object: {errors[-1]}")


class AgentAdapter(Protocol):
    name: str

    def invoke(
        self,
        *,
        role: str,
        request: dict[str, Any],
        workspace: Path,
        transcript_path: Path | None = None,
    ) -> dict[str, Any]:
        ...


class DeterministicFakeAdapter:
    name = "fake"

    def __init__(self, fixture_path: Path) -> None:
        self._fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    def invoke(
        self,
        *,
        role: str,
        request: dict[str, Any],
        workspace: Path,
        transcript_path: Path | None = None,
    ) -> dict[str, Any]:
        del request, transcript_path
        role_fixture = self._fixture.get(role)
        if not isinstance(role_fixture, dict):
            raise ValueError(f"fake adapter fixture has no object for role {role}")
        if "output" not in role_fixture:
            return role_fixture
        writes = role_fixture.get("writes", {})
        if not isinstance(writes, dict):
            raise ValueError(f"fake adapter writes for role {role} must be an object")
        workspace_root = workspace.resolve()
        for relative_path, content in writes.items():
            if not isinstance(relative_path, str) or not isinstance(content, str):
                raise ValueError("fake adapter writes must map paths to text")
            target = (workspace / relative_path).resolve()
            try:
                target.relative_to(workspace_root)
            except ValueError as error:
                raise ValueError(
                    f"fake adapter write escapes Workspace: {relative_path}"
                ) from error
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        output = role_fixture["output"]
        if not isinstance(output, dict):
            raise ValueError(f"fake adapter output for role {role} must be an object")
        return output


class CodexAdapter:
    name = "codex"

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable or os.environ.get("AGENTFLOW_CODEX", "codex")

    def invoke(
        self,
        *,
        role: str,
        request: dict[str, Any],
        workspace: Path,
        transcript_path: Path | None = None,
    ) -> dict[str, Any]:
        del transcript_path
        sandbox = "workspace-write" if role in {"builder", "tester"} else "read-only"
        prompt = role_prompt(role, request)
        with tempfile.TemporaryDirectory(prefix="agentflow-codex-") as temp_dir:
            temp_path = Path(temp_dir)
            schema_path = temp_path / "output-schema.json"
            output_path = temp_path / "output.json"
            schema_path.write_text(
                json.dumps(contract_schema(role), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    self._executable,
                    "-a",
                    "never",
                    "exec",
                    "--ephemeral",
                    "--color",
                    "never",
                    "-C",
                    str(workspace),
                    "-s",
                    sandbox,
                    "--output-schema",
                    str(schema_path),
                    "-o",
                    str(output_path),
                    prompt,
                ],
                text=True,
                capture_output=True,
                timeout=3600,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"Codex adapter failed for role {role}:\n{completed.stderr}"
                )
            return json.loads(output_path.read_text(encoding="utf-8"))


class CursorAdapter:
    name = "cursor"
    _MAX_OUTPUT_ATTEMPTS = 2

    def __init__(
        self,
        executable: str | None = None,
        *,
        data_dir: Path | None = None,
        model: str | None = None,
    ) -> None:
        self._executable = executable or os.environ.get("AGENTFLOW_CURSOR", "agent")
        self._data_dir = data_dir
        self._model = model
        self.last_resolved_model: str | None = None

    def resolve_model(self, role: str) -> str:
        _validate_role(role)
        if self._model is not None:
            return self._model
        environment_model = os.environ.get(
            f"AGENTFLOW_CURSOR_{role.upper()}_MODEL"
        )
        if environment_model:
            return environment_model
        if self._data_dir is not None:
            recorded = read_model_routing(self._data_dir).get(self.name, {})
            recorded_model = recorded.get(role)
            if recorded_model:
                return recorded_model
        return SUGGESTED_MODELS[self.name][role]

    def invoke(
        self,
        *,
        role: str,
        request: dict[str, Any],
        workspace: Path,
        transcript_path: Path | None = None,
    ) -> dict[str, Any]:
        model = self.resolve_model(role)
        self.last_resolved_model = model
        prompt = (
            role_prompt(role, request)
            + "\n\nReturn exactly one JSON object with no Markdown fences or "
            "commentary. It must satisfy this JSON Schema:\n"
            + json.dumps(contract_schema(role), indent=2, sort_keys=True)
        )
        last_contract_error: Exception | None = None
        transcript_file = None
        try:
            if transcript_path is not None:
                transcript_path.parent.mkdir(parents=True, exist_ok=True)
                transcript_file = transcript_path.open("w", encoding="utf-8")
            for attempt in range(1, self._MAX_OUTPUT_ATTEMPTS + 1):
                if transcript_file is not None:
                    transcript_file.write(
                        json.dumps(
                            {
                                "adapter": self.name,
                                "attempt": attempt,
                                "type": "agentflow_adapter_attempt",
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    )
                    transcript_file.flush()
                result_event = self._run_attempt(
                    role=role,
                    workspace=workspace,
                    model=model,
                    prompt=prompt,
                    transcript_file=transcript_file,
                )
                raw_output = result_event.get("result")
                try:
                    return _parse_cursor_output(role, raw_output)
                except (ContractError, json.JSONDecodeError, TypeError) as error:
                    last_contract_error = error
                    prompt += (
                        "\n\nYour previous response failed local validation: "
                        f"{error}. Return a corrected JSON object only. Do not "
                        "repeat or undo completed repository edits."
                    )
        finally:
            if transcript_file is not None:
                transcript_file.close()
        raise RuntimeError(
            f"Cursor adapter returned invalid structured output for role {role} "
            f"after {self._MAX_OUTPUT_ATTEMPTS} attempts: {last_contract_error}"
        )

    def _run_attempt(
        self,
        *,
        role: str,
        workspace: Path,
        model: str,
        prompt: str,
        transcript_file: TextIO | None,
    ) -> dict[str, Any]:
        arguments = [
            self._executable,
            "--print",
            "--output-format",
            "stream-json",
            "--workspace",
            str(workspace),
            "--trust",
            "--model",
            model,
        ]
        if role in {"planner", "reviewer"}:
            arguments.extend(["--mode", "ask"])
        else:
            arguments.extend(["--force", "--sandbox", "enabled"])
        arguments.append(prompt)
        process = subprocess.Popen(
            arguments,
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stderr_chunks: list[str] = []

        def _drain_stderr() -> None:
            assert process.stderr is not None
            stderr_chunks.append(process.stderr.read())

        stderr_reader = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_reader.start()
        result_event: dict[str, Any] | None = None
        assert process.stdout is not None
        for line in process.stdout:
            if transcript_file is not None:
                transcript_file.write(line)
                transcript_file.flush()
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                result_event = event
        try:
            returncode = process.wait(timeout=3600)
        except subprocess.TimeoutExpired:
            process.kill()
            raise
        stderr_reader.join()
        stderr_text = "".join(stderr_chunks)
        if returncode != 0:
            raise RuntimeError(
                f"Cursor adapter failed for role {role} with exit "
                f"{returncode}:\n{stderr_text}"
            )
        if result_event is None:
            raise RuntimeError(
                f"Cursor adapter returned no result event for role {role}"
            )
        if result_event.get("subtype") != "success":
            diagnostics = {
                key: result_event.get(key)
                for key in (
                    "subtype",
                    "duration_ms",
                    "duration_api_ms",
                    "session_id",
                )
                if result_event.get(key) is not None
            }
            raise RuntimeError(
                f"Cursor adapter reported failure for role {role}: "
                f"{json.dumps(diagnostics, sort_keys=True)}\n"
                f"{result_event.get('result')}"
            )
        return result_event


class ClaudeAdapter:
    name = "claude"

    _READ_ONLY_ARGUMENTS = [
        "--tools",
        "Read,Grep,Glob",
        "--permission-mode",
        "dontAsk",
    ]
    _WRITE_ARGUMENTS = [
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "Bash",
    ]
    _ROLE_ARGUMENTS = {
        "planner": _READ_ONLY_ARGUMENTS,
        "builder": _WRITE_ARGUMENTS,
        "reviewer": _READ_ONLY_ARGUMENTS,
        "tester": _WRITE_ARGUMENTS,
    }

    def __init__(
        self,
        executable: str | None = None,
        *,
        data_dir: Path | None = None,
        model: str | None = None,
    ) -> None:
        self._executable = executable or os.environ.get("AGENTFLOW_CLAUDE", "claude")
        self._data_dir = data_dir
        self._model = model
        self.last_resolved_model: str | None = None

    def resolve_model(self, role: str) -> str:
        _validate_role(role)
        if self._model is not None:
            return self._model
        environment_model = os.environ.get(
            f"AGENTFLOW_CLAUDE_{role.upper()}_MODEL"
        )
        if environment_model:
            return environment_model
        if self._data_dir is not None:
            recorded = read_model_routing(self._data_dir).get(self.name, {})
            recorded_model = recorded.get(role)
            if recorded_model:
                return recorded_model
        return SUGGESTED_MODELS[self.name][role]

    def invoke(
        self,
        *,
        role: str,
        request: dict[str, Any],
        workspace: Path,
        transcript_path: Path | None = None,
    ) -> dict[str, Any]:
        # Resolve the model exactly once per invocation so the value passed to
        # the CLI and the value stamped as event provenance can never diverge.
        model = self.resolve_model(role)
        self.last_resolved_model = model
        process = subprocess.Popen(
            [
                self._executable,
                "--print",
                "--output-format",
                "stream-json",
                "--verbose",
                "--json-schema",
                json.dumps(contract_schema(role), sort_keys=True),
                "--no-session-persistence",
                *self._ROLE_ARGUMENTS[role],
                "--model",
                model,
            ],
            cwd=workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Drain stderr on a separate thread so a full stderr pipe can never
        # deadlock the stdout streaming loop below.
        stderr_chunks: list[str] = []

        def _drain_stderr() -> None:
            assert process.stderr is not None
            stderr_chunks.append(process.stderr.read())

        stderr_reader = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_reader.start()

        result_event: dict[str, Any] | None = None
        transcript_file = None
        try:
            assert process.stdin is not None
            process.stdin.write(role_prompt(role, request))
            process.stdin.close()
            if transcript_path is not None:
                transcript_path.parent.mkdir(parents=True, exist_ok=True)
                transcript_file = transcript_path.open("w", encoding="utf-8")
            assert process.stdout is not None
            for line in process.stdout:
                if transcript_file is not None:
                    # Append and flush each stream line as it arrives so the
                    # transcript can be tailed live.
                    transcript_file.write(line)
                    transcript_file.flush()
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("type") == "result":
                    result_event = event
        finally:
            if transcript_file is not None:
                transcript_file.close()

        try:
            returncode = process.wait(timeout=3600)
        except subprocess.TimeoutExpired:
            process.kill()
            raise
        stderr_reader.join()
        stderr_text = "".join(stderr_chunks)
        if returncode != 0:
            diagnostics = (
                _claude_result_diagnostics(result_event)
                if result_event is not None
                else ""
            )
            raise RuntimeError(
                f"Claude adapter failed for role {role}{diagnostics}:\n{stderr_text}"
            )
        if result_event is None:
            raise RuntimeError(
                f"Claude adapter returned no structured output for role {role}"
            )
        if result_event.get("is_error") or result_event.get("subtype") != "success":
            raise RuntimeError(
                f"Claude adapter reported failure for role {role}"
                f"{_claude_result_diagnostics(result_event)}:\n"
                f"{result_event.get('result')}"
            )
        structured_output = result_event.get("structured_output")
        if not isinstance(structured_output, dict):
            raise RuntimeError(
                f"Claude adapter returned no structured output for role {role}"
            )
        return structured_output
