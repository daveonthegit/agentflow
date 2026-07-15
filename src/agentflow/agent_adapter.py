from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Protocol

from .contracts import contract_schema


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
        role_instruction = {
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
        }[role]
        prompt = (
            f"You are the Agentflow {role} Agent Role.\n\n"
            f"{role_instruction}\n\n"
            "Workflow context:\n"
            + json.dumps(request, indent=2, sort_keys=True)
        )
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
