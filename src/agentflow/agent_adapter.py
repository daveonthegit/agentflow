from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Protocol

from .contracts import contract_schema


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
}

ROLES = ("planner", "builder", "reviewer")

SUGGESTED_MODELS = {
    "claude": {"builder": "opus", "planner": "fable", "reviewer": "opus"},
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


class AgentAdapter(Protocol):
    name: str

    def invoke(
        self,
        *,
        role: str,
        request: dict[str, Any],
        workspace: Path,
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
    ) -> dict[str, Any]:
        del request
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
    ) -> dict[str, Any]:
        sandbox = "workspace-write" if role == "builder" else "read-only"
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


class ClaudeAdapter:
    name = "claude"

    _READ_ONLY_ARGUMENTS = [
        "--tools",
        "Read,Grep,Glob",
        "--permission-mode",
        "dontAsk",
    ]
    _ROLE_ARGUMENTS = {
        "planner": _READ_ONLY_ARGUMENTS,
        "builder": [
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            "Bash",
        ],
        "reviewer": _READ_ONLY_ARGUMENTS,
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
    ) -> dict[str, Any]:
        model = self.resolve_model(role)
        completed = subprocess.run(
            [
                self._executable,
                "--print",
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(contract_schema(role), sort_keys=True),
                "--no-session-persistence",
                *self._ROLE_ARGUMENTS[role],
                "--model",
                model,
            ],
            input=role_prompt(role, request),
            cwd=workspace,
            text=True,
            capture_output=True,
            timeout=3600,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"Claude adapter failed for role {role}:\n{completed.stderr}"
            )
        envelope = json.loads(completed.stdout)
        if envelope.get("is_error") or envelope.get("subtype") != "success":
            raise RuntimeError(
                f"Claude adapter reported failure for role {role}:\n"
                f"{envelope.get('result')}"
            )
        structured_output = envelope.get("structured_output")
        if not isinstance(structured_output, dict):
            raise RuntimeError(
                f"Claude adapter returned no structured output for role {role}"
            )
        return structured_output
