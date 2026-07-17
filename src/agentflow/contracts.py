from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from typing import Any


class ContractError(ValueError):
    pass


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


_WORK_ITEM_FIELDS = ("id", "summary", "acceptance_criteria", "depends_on", "status")
WORK_ITEM_STATUS_PROPOSED = "proposed"


def _validate_string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ContractError(f"{label} must be a list of strings")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ContractError(f"{label} must be a list of strings")
        trimmed = item.strip()
        if not trimmed:
            raise ContractError(f"{label} must not contain blank strings")
        if trimmed in seen:
            raise ContractError(f"{label} must not contain duplicates")
        seen.add(trimmed)
        result.append(trimmed)
    return result


def validate_work_item(value: Any) -> dict[str, Any]:
    """Validate one Work Item.

    A Work Item is one unit of intended work in a Target Repository's Work
    Graph. ``depends_on`` lists the ids of Work Items that must complete first;
    ready work is computed from these relationships rather than stored. Unknown
    fields are rejected so the schema stays explicit and versionable.

    ``status`` is optional and, when present, may only be ``"proposed"``: the
    marker for an item appended from a validated Discovery that has not passed
    human Framing approval. An absent ``status`` means the item belongs to the
    approved graph.
    """
    if not isinstance(value, dict):
        raise ContractError("work item must be an object")
    unknown = set(value) - set(_WORK_ITEM_FIELDS)
    if unknown:
        raise ContractError(f"work item contains unknown fields: {sorted(unknown)}")
    for field in ("id", "summary"):
        if field not in value:
            raise ContractError(f"work item {field} is required")
        if not isinstance(value[field], str) or not value[field].strip():
            raise ContractError(f"work item {field} must be a non-empty string")
    criteria = _validate_string_list(
        value.get("acceptance_criteria", []), "work item acceptance_criteria"
    )
    depends_on = _validate_string_list(
        value.get("depends_on", []), "work item depends_on"
    )
    item_id = value["id"].strip()
    if item_id in depends_on:
        raise ContractError(f"work item {item_id} cannot depend on itself")
    item: dict[str, Any] = {
        "id": item_id,
        "summary": value["summary"].strip(),
        "acceptance_criteria": criteria,
        "depends_on": depends_on,
    }
    if "status" in value:
        if value["status"] != WORK_ITEM_STATUS_PROPOSED:
            raise ContractError(
                f"work item status may only be '{WORK_ITEM_STATUS_PROPOSED}'"
            )
        item["status"] = WORK_ITEM_STATUS_PROPOSED
    return item


def validate_work_graph(items: Any) -> list[dict[str, Any]]:
    """Validate a whole Work Graph: unique ids, resolvable deps, no cycles."""
    if not isinstance(items, list):
        raise ContractError("work graph must be a list of work items")
    validated = [validate_work_item(item) for item in items]
    ids = [item["id"] for item in validated]
    duplicates = sorted({item_id for item_id in ids if ids.count(item_id) > 1})
    if duplicates:
        raise ContractError(f"work graph has duplicate ids: {duplicates}")
    id_set = set(ids)
    for item in validated:
        missing = sorted(dep for dep in item["depends_on"] if dep not in id_set)
        if missing:
            raise ContractError(
                f"work item {item['id']} depends on unknown ids: {missing}"
            )
    _reject_dependency_cycles(validated)
    return validated


def _reject_dependency_cycles(items: list[dict[str, Any]]) -> None:
    graph = {item["id"]: item["depends_on"] for item in items}
    # 0 = unvisited, 1 = on the current DFS path, 2 = fully explored.
    state: dict[str, int] = {}

    def visit(node: str) -> None:
        state[node] = 1
        for dependency in graph[node]:
            if state.get(dependency, 0) == 1:
                raise ContractError(
                    f"work graph has a dependency cycle involving {node}"
                )
            if state.get(dependency, 0) == 0:
                visit(dependency)
        state[node] = 2

    for item_id in graph:
        if state.get(item_id, 0) == 0:
            visit(item_id)


# Hard cap on Discoveries per validated role output. Enforced here in
# deterministic validation, never left to adapters or models.
MAX_DISCOVERIES_PER_OUTPUT = 10

_DISCOVERY_FIELDS = ("key", "summary", "acceptance_criteria", "depends_on")


def validate_discovery(value: Any) -> dict[str, Any]:
    """Validate one Discovery.

    A Discovery is a structured finding returned by an Agent Role as part of
    its validated output contract. ``key`` is the dedup key and becomes the
    proposed Work Item id when the engine applies the Discovery to the Work
    Graph. Unknown fields are rejected so the schema stays explicit.
    """
    if not isinstance(value, dict):
        raise ContractError("discovery must be an object")
    unknown = set(value) - set(_DISCOVERY_FIELDS)
    if unknown:
        raise ContractError(f"discovery contains unknown fields: {sorted(unknown)}")
    for field in ("key", "summary"):
        if field not in value:
            raise ContractError(f"discovery {field} is required")
        if not isinstance(value[field], str) or not value[field].strip():
            raise ContractError(f"discovery {field} must be a non-empty string")
    criteria = _validate_string_list(
        value.get("acceptance_criteria", []), "discovery acceptance_criteria"
    )
    depends_on = _validate_string_list(
        value.get("depends_on", []), "discovery depends_on"
    )
    key = value["key"].strip()
    if key in depends_on:
        raise ContractError(f"discovery {key} cannot depend on itself")
    return {
        "key": key,
        "summary": value["summary"].strip(),
        "acceptance_criteria": criteria,
        "depends_on": depends_on,
    }


def validate_discoveries(value: Any) -> list[dict[str, Any]]:
    """Validate a role output's Discoveries list: capped and dedup-keyed.

    At most ``MAX_DISCOVERIES_PER_OUTPUT`` Discoveries per output, and every
    ``key`` must be unique within the output. Both limits are deterministic
    validation errors, not silent truncation.
    """
    if not isinstance(value, list):
        raise ContractError("discoveries must be a list")
    if len(value) > MAX_DISCOVERIES_PER_OUTPUT:
        raise ContractError(
            f"discoveries must contain at most {MAX_DISCOVERIES_PER_OUTPUT} "
            f"entries, got {len(value)}"
        )
    validated = [validate_discovery(item) for item in value]
    keys = [item["key"] for item in validated]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ContractError(f"discoveries have duplicate keys: {duplicates}")
    return validated


def _discoveries_schema() -> dict[str, Any]:
    return {
        "description": (
            "Optional structured findings proposing future Work Items. "
            f"At most {MAX_DISCOVERIES_PER_OUTPUT} per output, each with a "
            "unique key. Discoveries are applied to the Work Graph only by "
            "deterministic engine validation; never write .agentflow/work/ "
            "files directly."
        ),
        "items": {
            "additionalProperties": False,
            "properties": {
                "acceptance_criteria": {
                    "items": {"type": "string"},
                    "type": "array",
                },
                "depends_on": {"items": {"type": "string"}, "type": "array"},
                "key": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["key", "summary"],
            "type": "object",
        },
        "maxItems": MAX_DISCOVERIES_PER_OUTPUT,
        "type": "array",
    }


def contract_schema(role: str) -> dict[str, Any]:
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
        # A non-empty list fails the stage gate; keep this description in the
        # schema so adapters that pass --json-schema surface it to the model.
        fields["unresolved_issues"] = {
            "items": {"type": "string"},
            "type": "array",
            "description": (
                "Must be empty unless an acceptance criterion remains unmet. "
                "Do not list minor notes, follow-ups, or out-of-scope items; "
                "a non-empty list fails the stage."
            ),
        }
        required = list(fields)
        fields["discoveries"] = _discoveries_schema()
        return {
            "additionalProperties": False,
            "properties": fields,
            "required": required,
            "type": "object",
        }
    if role == "reviewer":
        return {
            "additionalProperties": False,
            "properties": {
                "discoveries": _discoveries_schema(),
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
                "discoveries": _discoveries_schema(),
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


def validate_builder_report(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("builder report must be an object")
    required = {
        "commands_run",
        "files_changed",
        "steps_completed",
        "unresolved_issues",
    }
    if set(value) - required - {"discoveries"} or required - set(value):
        raise ContractError(
            f"builder report fields must be exactly {sorted(required)} "
            "plus optional discoveries"
        )
    for field in required:
        if not isinstance(value[field], list) or not all(
            isinstance(item, str) for item in value[field]
        ):
            raise ContractError(f"builder report {field} must be a list of strings")
    if "discoveries" in value:
        return {**value, "discoveries": validate_discoveries(value["discoveries"])}
    return value


def validate_tester_report(value: Any) -> dict[str, Any]:
    """Validate a Tester Agent Role output, version 1.

    ``findings`` are evidence only: they are recorded and surfaced to the
    reviewer but never gate the workflow. Only failing tests the tester writes
    (run by the authoritative checks) change Run State. ``file`` may be null for
    a global finding, consistent with the reviewer contract.
    """
    required = {"summary", "files_changed", "findings"}
    if (
        not isinstance(value, dict)
        or set(value) - required - {"discoveries"}
        or required - set(value)
    ):
        raise ContractError(
            "tester report must contain summary, files_changed, findings "
            "and optionally discoveries"
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
    if "discoveries" in value:
        return {**value, "discoveries": validate_discoveries(value["discoveries"])}
    return value


# A proposals-inbox file is Discoveries-from-foreign-agents: any agent may drop
# a JSON proposal into the quarantined ``.agentflow/proposals/`` directory, and
# it reaches the Work Graph only through the same deterministic validation the
# in-band Discoveries machinery uses. The per-ingest cap mirrors the Discoveries
# cap so a foreign writer can never flood the graph in one pass.
MAX_PROPOSALS_PER_INGEST = MAX_DISCOVERIES_PER_OUTPUT

PROPOSAL_KIND_NEW_WORK = "new-work"
PROPOSAL_KIND_COMPLETION_CLAIM = "completion-claim"
PROPOSAL_KINDS = (PROPOSAL_KIND_NEW_WORK, PROPOSAL_KIND_COMPLETION_CLAIM)

_PROPOSAL_FIELDS = ("kind", "summary", "acceptance_criteria", "relates_to")


def validate_proposal(value: Any) -> dict[str, Any]:
    """Validate one quarantined proposals-inbox file.

    A proposal is JSON any foreign agent may write into
    ``.agentflow/proposals/``. ``kind`` is either ``new-work`` (a suggested
    future Work Item) or ``completion-claim`` (an assertion that work is
    already done, which only a human may act on). ``summary`` is required for
    both; ``acceptance_criteria`` is a required non-empty list for new-work and
    optional for a completion-claim; ``relates_to`` is an optional list of
    Work-Item ids the proposal references. Unknown fields are rejected so the
    entry door stays explicit and versionable — writing an ill-formed file is a
    reported defect, never a graph mutation.
    """
    if not isinstance(value, dict):
        raise ContractError("proposal must be an object")
    unknown = set(value) - set(_PROPOSAL_FIELDS)
    if unknown:
        raise ContractError(f"proposal contains unknown fields: {sorted(unknown)}")
    kind = value.get("kind")
    if kind not in PROPOSAL_KINDS:
        raise ContractError(
            f"proposal kind must be one of {sorted(PROPOSAL_KINDS)}"
        )
    if not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise ContractError("proposal summary must be a non-empty string")
    criteria = _validate_string_list(
        value.get("acceptance_criteria", []), "proposal acceptance_criteria"
    )
    if kind == PROPOSAL_KIND_NEW_WORK and not criteria:
        raise ContractError(
            "new-work proposal requires at least one acceptance criterion"
        )
    relates_to = _validate_string_list(
        value.get("relates_to", []), "proposal relates_to"
    )
    return {
        "kind": kind,
        "summary": value["summary"].strip(),
        "acceptance_criteria": criteria,
        "relates_to": relates_to,
    }


def proposal_work_item_id(proposal: dict[str, Any]) -> str:
    """Content-derived stable id for a new-work proposal's Work Item.

    Derived only from ``kind`` and ``summary`` (matching the improvement-proposal
    precedent) so the same suggestion always maps to the same proposed Work Item
    id, which is what lets ingest dedup a re-dropped proposal against the graph
    and against its siblings in the same pass.
    """
    payload = json.dumps(
        {"kind": proposal["kind"], "summary": proposal["summary"]},
        sort_keys=True,
    )
    return "proposal-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def validate_review(value: Any) -> dict[str, Any]:
    required = {"disposition", "findings"}
    if (
        not isinstance(value, dict)
        or set(value) - required - {"discoveries"}
        or required - set(value)
    ):
        raise ContractError(
            "review must contain disposition and findings "
            "and optionally discoveries"
        )
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
    if "discoveries" in value:
        return {**value, "discoveries": validate_discoveries(value["discoveries"])}
    return value
