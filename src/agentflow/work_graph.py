"""Target-repository Work Graph: git-tracked Work Items and computed ready work.

The Work Graph is owned by the Target Repository and stored as git-tracked JSONL
under ``.agentflow/work/``. Work-intent truth lives here; execution truth lives
in Run Evidence. The two keep only references to each other — a Run records the
``work_item_id`` it captured, and completion is derived from Run Evidence rather
than stored back into the graph. Ready work is computed from dependency
relationships whenever it is needed, never persisted as a mutable value.

Persistence goes through a replaceable ``WorkGraphBackend``. The default is the
native JSONL store; an in-memory backend exists for tests and adapters. Backend
swaps change only storage — validation and ready-work semantics stay in this
module.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Protocol

from .contracts import ContractError, validate_work_graph
from .run_kernel import COMPLETED_RUN_STATES, list_runs

WORK_RELATIVE_DIR = Path(".agentflow/work")
DEFAULT_GRAPH_FILENAME = "graph.jsonl"


class WorkGraphBackend(Protocol):
    """Replaceable persistence for Work Graph items.

    Backends own storage only. Callers validate with ``validate_work_graph``
    before write and after read so swapping implementations cannot change
    Work Graph semantics.
    """

    def read_items(self) -> list[dict]:
        """Return stored Work Item dicts, without validating the graph."""
        ...

    def write_items(self, items: list[dict]) -> None:
        """Fully replace the stored Work Item set with ``items``."""
        ...


class InMemoryWorkGraphBackend:
    """Full-replace in-memory Work Graph store.

    Deep-copies on read and write so nested fields such as ``depends_on`` and
    ``acceptance_criteria`` stay isolated from callers, matching JSONL round-trip
    isolation via serialization.
    """

    def __init__(self, items: list[dict] | None = None) -> None:
        self._items: list[dict] = [copy.deepcopy(item) for item in (items or [])]

    def read_items(self) -> list[dict]:
        return [copy.deepcopy(item) for item in self._items]

    def write_items(self, items: list[dict]) -> None:
        self._items = [copy.deepcopy(item) for item in items]


class JsonlWorkGraphBackend:
    """Native JSONL Work Graph store under ``.agentflow/work/``.

    Reads every ``*.jsonl`` file in deterministic order. ``write_items`` deletes
    all existing ``*.jsonl`` files and writes the replacement set to
    ``graph.jsonl``, matching the in-memory backend's full-replace semantics.
    """

    def __init__(self, repository: Path) -> None:
        self._repository = repository
        self._work_dir = repository / WORK_RELATIVE_DIR

    def read_items(self) -> list[dict]:
        if not self._work_dir.is_dir():
            return []
        items: list[dict] = []
        for path in sorted(self._work_dir.glob("*.jsonl")):
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise ContractError(
                        f"{path.name}:{line_number} is not valid JSON"
                    ) from error
        return items

    def write_items(self, items: list[dict]) -> None:
        self._work_dir.mkdir(parents=True, exist_ok=True)
        for path in self._work_dir.glob("*.jsonl"):
            path.unlink()
        if not items:
            return
        target = self._work_dir / DEFAULT_GRAPH_FILENAME
        target.write_text(
            "".join(
                json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
                for item in items
            ),
            encoding="utf-8",
        )


def default_work_graph_backend(repository: Path) -> JsonlWorkGraphBackend:
    """Default Work Graph persistence for a Target Repository."""
    return JsonlWorkGraphBackend(repository)


def work_item_content_hash(item: dict) -> str:
    """Stable content hash of a Work Item for Run capture-by-reference.

    A Run records this alongside the ``work_item_id`` so later edits to the Work
    Item are detectable and never silently alter an in-flight Run.
    """
    canonical = json.dumps(item, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_work_graph(
    repository: Path | None = None,
    *,
    backend: WorkGraphBackend | None = None,
) -> list[dict]:
    """Load and validate the Work Graph via a Work Graph backend.

    Uses the JSONL store for ``repository`` when ``backend`` is omitted.
    Validation (unique ids, resolvable dependencies, no cycles) always runs
    here so backend swaps cannot change graph semantics. A missing store is an
    empty graph.
    """
    store = backend if backend is not None else default_work_graph_backend(
        repository if repository is not None else Path.cwd()
    )
    return validate_work_graph(store.read_items())


def save_work_graph(
    items: list[dict],
    repository: Path | None = None,
    *,
    backend: WorkGraphBackend | None = None,
) -> list[dict]:
    """Validate then fully replace the Work Graph via a Work Graph backend.

    Uses the JSONL store for ``repository`` when ``backend`` is omitted.
    ``write_items`` replaces the entire stored set; validation runs before the
    write so invalid graphs never persist and backend swaps cannot change
    semantics.
    """
    validated = validate_work_graph(items)
    store = backend if backend is not None else default_work_graph_backend(
        repository if repository is not None else Path.cwd()
    )
    store.write_items(validated)
    return validated


def completed_work_item_ids(data_dir: Path) -> set[str]:
    """Work-item ids a human-approved Run has already delivered.

    Completion is read from Run Evidence: a Work Item is done when a
    ``human_approved`` (or subsequently ``merged``) Run captured it (its Task
    Spec ``source.work_item_id`` names the item). Nothing is written back to
    the Work Graph.
    """
    completed: set[str] = set()
    for run in list_runs(data_dir=data_dir):
        if run.state not in COMPLETED_RUN_STATES:
            continue
        source = run.source
        if isinstance(source, dict) and source.get("work_item_id"):
            completed.add(source["work_item_id"])
    return completed


def compute_ready_work(
    graph: list[dict], completed_ids: set[str]
) -> list[dict]:
    """Work Items that are not yet complete and whose dependencies all are.

    Deterministic: the result preserves the graph's order. Ready work is derived
    on demand from the dependency relationships and the completion set; it is
    never stored.
    """
    ready: list[dict] = []
    for item in graph:
        if item["id"] in completed_ids:
            continue
        if all(dep in completed_ids for dep in item["depends_on"]):
            ready.append(item)
    return ready
