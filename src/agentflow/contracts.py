from __future__ import annotations

from datetime import datetime
from pathlib import Path, PurePosixPath
import re
from typing import Any


class ContractError(ValueError):
    pass


MIN_PLAN_TEXT_LENGTH = 20
_CONTENT_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TASK_SOURCE_FIELDS = ("provider", "work_item_id", "captured_at", "content_hash")


def _require_aware_iso8601(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label} must be a non-empty ISO-8601 timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ContractError(
            f"{label} must be a valid ISO-8601 timestamp with an explicit timezone"
        ) from error
    if parsed.tzinfo is None:
        raise ContractError(f"{label} must include an explicit timezone")


def validate_task_source(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ContractError("task source must be an object")
    if set(value) != set(_TASK_SOURCE_FIELDS):
        raise ContractError(
            "task source fields must be exactly "
            f"{sorted(_TASK_SOURCE_FIELDS)}"
        )
    for field in _TASK_SOURCE_FIELDS:
        if not isinstance(value[field], str) or not value[field].strip():
            raise ContractError(f"task source {field} must be a non-empty string")
    _require_aware_iso8601(value["captured_at"], "task source captured_at")
    if not _CONTENT_HASH_PATTERN.fullmatch(value["content_hash"]):
        raise ContractError(
            "task source content_hash must be exactly 64 lowercase hexadecimal characters"
        )
    return {
        "provider": value["provider"],
        "work_item_id": value["work_item_id"],
        "captured_at": value["captured_at"],
        "content_hash": value["content_hash"],
    }


def validate_task_spec(value: Any) -> dict[str, Any]:
    """Validate a Task Spec, including legacy summary-only objects.

    New Runs always persist ``acceptance_criteria`` (empty allowed). ``source``
    is optional and omitted unless supplied. Unknown fields are rejected.
    ``content_hash`` is an importer-supplied upstream reference and is never
    recomputed from task.json.
    """
    if not isinstance(value, dict):
        raise ContractError("task must be an object")
    allowed = {"summary", "acceptance_criteria", "source"}
    unknown = set(value) - allowed
    if unknown:
        raise ContractError(
            f"task contains unknown fields: {sorted(unknown)}"
        )
    if "summary" not in value:
        raise ContractError("task summary is required")
    if not isinstance(value["summary"], str) or not value["summary"].strip():
        raise ContractError("task summary must be a non-empty string")
    criteria_raw = value.get("acceptance_criteria", [])
    if not isinstance(criteria_raw, list):
        raise ContractError("task acceptance_criteria must be a list of strings")
    criteria: list[str] = []
    seen: set[str] = set()
    for item in criteria_raw:
        if not isinstance(item, str):
            raise ContractError("task acceptance_criteria must be a list of strings")
        trimmed = item.strip()
        if not trimmed:
            raise ContractError(
                "task acceptance_criteria must not contain blank strings"
            )
        if trimmed in seen:
            raise ContractError(
                "task acceptance_criteria must not contain duplicates"
            )
        seen.add(trimmed)
        criteria.append(trimmed)
    task: dict[str, Any] = {
        "summary": value["summary"].strip(),
        "acceptance_criteria": criteria,
    }
    if "source" in value:
        task["source"] = validate_task_source(value["source"])
    return task



def contract_schema(role: str) -> dict[str, Any]:
    if role == "planner":
        return {
            "additionalProperties": False,
            "properties": {
                "files_to_modify": {
                    "items": {"type": "string"},
                    "minItems": 1,
                    "type": "array",
                },
                "risks": {"items": {"type": "string"}, "type": "array"},
                "steps": {
                    "items": {
                        "additionalProperties": False,
                        "properties": {
                            "description": {"type": "string"},
                            "id": {"type": "string"},
                            "verification": {"type": "string"},
                        },
                        "required": ["description", "id", "verification"],
                        "type": "object",
                    },
                    "type": "array",
                },
                "summary": {"type": "string"},
            },
            "required": ["files_to_modify", "risks", "steps", "summary"],
            "type": "object",
        }
    if role == "builder":
        fields = {
            name: {"items": {"type": "string"}, "type": "array"}
            for name in (
                "commands_run",
                "files_changed",
                "steps_completed",
                "unresolved_issues",
            )
        }
        return {
            "additionalProperties": False,
            "properties": fields,
            "required": list(fields),
            "type": "object",
        }
    if role == "reviewer":
        return {
            "additionalProperties": False,
            "properties": {
                "disposition": {
                    "enum": ["approve", "changes_requested"],
                    "type": "string",
                },
                "findings": {
                    "items": {
                        "additionalProperties": False,
                        "properties": {
                            "file": {"type": ["string", "null"]},
                            "message": {"type": "string"},
                            "severity": {
                                "enum": ["blocker", "major", "minor", "note"],
                                "type": "string",
                            },
                        },
                        "required": ["file", "message", "severity"],
                        "type": "object",
                    },
                    "type": "array",
                },
            },
            "required": ["disposition", "findings"],
            "type": "object",
        }
    if role == "tester":
        return {
            "additionalProperties": False,
            "properties": {
                "summary": {"type": "string"},
                "files_changed": {"items": {"type": "string"}, "type": "array"},
                "findings": {
                    "items": {
                        "additionalProperties": False,
                        "properties": {
                            "file": {"type": ["string", "null"]},
                            "message": {"type": "string"},
                            "severity": {
                                "enum": ["blocker", "major", "minor", "note"],
                                "type": "string",
                            },
                        },
                        "required": ["file", "message", "severity"],
                        "type": "object",
                    },
                    "type": "array",
                },
            },
            "required": ["summary", "files_changed", "findings"],
            "type": "object",
        }
    raise ValueError(f"no output contract for role {role}")


def _require_plan_substance(label: str, text: str) -> None:
    if len(text.strip()) < MIN_PLAN_TEXT_LENGTH:
        raise ContractError(
            f"plan {label} must contain at least {MIN_PLAN_TEXT_LENGTH} characters"
        )


def validate_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("plan must be an object")
    required = {"summary", "files_to_modify", "steps", "risks"}
    if set(value) != required:
        raise ContractError(f"plan fields must be exactly {sorted(required)}")
    if not isinstance(value["summary"], str) or not value["summary"].strip():
        raise ContractError("plan summary must be a non-empty string")
    _require_plan_substance("summary", value["summary"])
    if not isinstance(value["files_to_modify"], list) or not value["files_to_modify"]:
        raise ContractError("plan files_to_modify must be a non-empty list of paths")
    if not all(
        isinstance(path, str) and path for path in value["files_to_modify"]
    ):
        raise ContractError("plan files_to_modify must be a list of paths")
    if len(set(value["files_to_modify"])) != len(value["files_to_modify"]):
        raise ContractError("plan files_to_modify must not contain duplicates")
    for path in value["files_to_modify"]:
        planned_path = PurePosixPath(path)
        if planned_path.is_absolute() or ".." in planned_path.parts:
            raise ContractError(
                "plan files_to_modify paths must stay within the Workspace"
            )
    if not isinstance(value["risks"], list) or not all(
        isinstance(risk, str) and risk for risk in value["risks"]
    ):
        raise ContractError("plan risks must be a list of strings")
    if not isinstance(value["steps"], list) or not value["steps"]:
        raise ContractError("plan steps must be a non-empty list")
    for step in value["steps"]:
        if not isinstance(step, dict) or set(step) != {
            "id",
            "description",
            "verification",
        }:
            raise ContractError(
                "each plan step must contain id, description, and verification"
            )
        if not all(isinstance(step[field], str) and step[field] for field in step):
            raise ContractError("plan step fields must be non-empty strings")
        _require_plan_substance("step description", step["description"])
        _require_plan_substance("step verification", step["verification"])
    return value


def validate_planned_paths(*, plan: dict[str, Any], workspace: Path) -> None:
    """Reject planned paths that are not creatable or editable in the Workspace.

    Call only from workflow code that has a Workspace. Adapter-local
    ``validate_plan`` intentionally skips filesystem checks so providers can
    validate shape without a checkout.
    """
    workspace = workspace.resolve()
    for path in plan["files_to_modify"]:
        planned_path = PurePosixPath(path)
        if planned_path.is_absolute() or ".." in planned_path.parts:
            raise ContractError(
                "plan files_to_modify paths must stay within the Workspace"
            )
        target = (workspace / path).resolve()
        try:
            target.relative_to(workspace)
        except ValueError as error:
            raise ContractError(
                "plan files_to_modify paths must stay within the Workspace"
            ) from error
        if target.exists():
            if not target.is_file():
                raise ContractError(
                    f"plan files_to_modify path is not a regular file: {path}"
                )
            continue
        parent = target.parent
        if not parent.is_dir():
            raise ContractError(
                f"plan files_to_modify parent directory does not exist: {path}"
            )


def validate_builder_report(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("builder report must be an object")
    required = {
        "commands_run",
        "files_changed",
        "steps_completed",
        "unresolved_issues",
    }
    if set(value) != required:
        raise ContractError(
            f"builder report fields must be exactly {sorted(required)}"
        )
    for field in required:
        if not isinstance(value[field], list) or not all(
            isinstance(item, str) for item in value[field]
        ):
            raise ContractError(f"builder report {field} must be a list of strings")
    return value


def validate_tester_report(value: Any) -> dict[str, Any]:
    """Validate a Tester Agent Role output, version 1.

    ``findings`` are evidence only: they are recorded and surfaced to the
    reviewer but never gate the workflow. Only failing tests the tester writes
    (run by the authoritative checks) change Run State. ``file`` may be null for
    a global finding, consistent with the reviewer contract.
    """
    if not isinstance(value, dict) or set(value) != {
        "summary",
        "files_changed",
        "findings",
    }:
        raise ContractError(
            "tester report must contain summary, files_changed, findings"
        )
    if not isinstance(value["summary"], str) or not value["summary"].strip():
        raise ContractError("tester report summary must be a non-empty string")
    if not isinstance(value["files_changed"], list) or not all(
        isinstance(path, str) and path for path in value["files_changed"]
    ):
        raise ContractError("tester report files_changed must be a list of paths")
    if not isinstance(value["findings"], list):
        raise ContractError("tester report findings must be a list")
    for finding in value["findings"]:
        if not isinstance(finding, dict) or set(finding) != {
            "file",
            "message",
            "severity",
        }:
            raise ContractError("each tester finding must contain file, message, severity")
        if finding["severity"] not in {"blocker", "major", "minor", "note"}:
            raise ContractError("tester finding severity is invalid")
        if not isinstance(finding["message"], str) or not finding["message"]:
            raise ContractError("tester finding message must be non-empty")
        if finding["file"] is not None and not isinstance(finding["file"], str):
            raise ContractError("tester finding file must be a path or null")
    return value


def validate_review(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"disposition", "findings"}:
        raise ContractError("review must contain disposition and findings")
    if value["disposition"] not in {"approve", "changes_requested"}:
        raise ContractError("review disposition is invalid")
    if not isinstance(value["findings"], list):
        raise ContractError("review findings must be a list")
    for finding in value["findings"]:
        if not isinstance(finding, dict) or set(finding) != {
            "file",
            "message",
            "severity",
        }:
            raise ContractError("each review finding must contain file, message, severity")
        if finding["severity"] not in {"blocker", "major", "minor", "note"}:
            raise ContractError("review finding severity is invalid")
        if not isinstance(finding["message"], str) or not finding["message"]:
            raise ContractError("review finding message must be non-empty")
        if finding["file"] is not None and not isinstance(finding["file"], str):
            raise ContractError("review finding file must be a path or null")
    return value
