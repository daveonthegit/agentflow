from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any


class ContractError(ValueError):
    pass


def contract_schema(role: str) -> dict[str, Any]:
    if role == "planner":
        return {
            "additionalProperties": False,
            "properties": {
                "files_to_modify": {
                    "items": {"type": "string"},
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
    raise ValueError(f"no output contract for role {role}")


def validate_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("plan must be an object")
    required = {"summary", "files_to_modify", "steps", "risks"}
    if set(value) != required:
        raise ContractError(f"plan fields must be exactly {sorted(required)}")
    if not isinstance(value["summary"], str) or not value["summary"].strip():
        raise ContractError("plan summary must be a non-empty string")
    if not isinstance(value["files_to_modify"], list) or not all(
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
    return value


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
