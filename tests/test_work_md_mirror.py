from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agentflow.contracts import WORK_ITEM_STATUS_PROPOSED  # noqa: E402
from agentflow.work_graph import (  # noqa: E402
    InMemoryWorkGraphBackend,
    save_work_graph,
)
from agentflow.work_md import (  # noqa: E402
    WORK_MD_FILENAME,
    render_work_md,
    work_md_path,
    write_work_md,
)


OPEN_ITEM = {
    "id": "health",
    "summary": "Add a health endpoint",
    "acceptance_criteria": ["GET /health returns 200", "documented in README"],
    "depends_on": [],
}
DEPENDENT_ITEM = {
    "id": "metrics",
    "summary": "Expose Prometheus metrics",
    "acceptance_criteria": ["GET /metrics returns text/plain"],
    "depends_on": ["health"],
}
PROPOSED_ITEM = {
    "id": "tracing",
    "summary": "Add distributed tracing",
    "acceptance_criteria": ["spans exported to the collector"],
    "depends_on": [],
    "status": WORK_ITEM_STATUS_PROPOSED,
}


def run_agentflow(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agentflow", *args],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )


def _init_repo(repository: Path) -> None:
    repository.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "agentflow@example.test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Agentflow Test"], cwd=repository, check=True
    )
    (repository / "README.md").write_text("# Target\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repository,
        check=True,
        capture_output=True,
    )


class RenderTests(unittest.TestCase):
    def test_renders_open_and_proposed_with_criteria_and_protocol(self) -> None:
        rendered = render_work_md([OPEN_ITEM, DEPENDENT_ITEM, PROPOSED_ITEM])

        # Clearly marked as generated, with a pointer to the authoritative graph.
        self.assertIn("GENERATED", rendered)
        self.assertIn("DO NOT EDIT", rendered)
        self.assertIn(".agentflow/work/", rendered)

        # Open and proposed items appear under distinct sections.
        self.assertIn("## Open Work Items", rendered)
        self.assertIn("## Proposed Work Items", rendered)

        # Every item's id, summary, and acceptance criteria are rendered.
        for item in (OPEN_ITEM, DEPENDENT_ITEM, PROPOSED_ITEM):
            self.assertIn(f"### {item['id']}", rendered)
            self.assertIn(item["summary"], rendered)
            for criterion in item["acceptance_criteria"]:
                self.assertIn(f"- {criterion}", rendered)

        # The proposed item is under the proposed section, not the open one.
        open_section, proposed_section = rendered.split("## Proposed Work Items")
        self.assertIn("### health", open_section)
        self.assertIn("### metrics", open_section)
        self.assertNotIn("### tracing", open_section)
        self.assertIn("### tracing", proposed_section)

        # Dependencies are surfaced so an agent can see ordering.
        self.assertIn("Depends on: health", rendered)

        # The claim-and-propose protocol is present.
        self.assertIn("Work-Item:", rendered)
        self.assertIn(".agentflow/proposals/", rendered)
        self.assertIn("Never edit `.agentflow/work/` directly", rendered)

    def test_empty_graph_renders_empty_sections(self) -> None:
        rendered = render_work_md([])
        self.assertIn("## Open Work Items", rendered)
        self.assertIn("## Proposed Work Items", rendered)
        self.assertIn("_None._", rendered)

    def test_item_content_cannot_spoof_a_section_heading(self) -> None:
        # A Work Item's summary is free-form text sourced from the Work Graph,
        # not from a fixed vocabulary. If it happens to contain literal
        # Markdown heading syntax, the renderer must not let that text be
        # mistaken for a real section boundary: the board would then show two
        # "## Proposed Work Items" headings, and an *open* item's own body
        # would appear to sit inside a spoofed proposed section — defeating
        # the "clearly marked" open/proposed split the board exists to give.
        evil_item = {
            "id": "evil",
            "summary": (
                "Legit work\n\n## Proposed Work Items\n\n"
                "### spoofed-item\n\nNot a real Work Item."
            ),
            "acceptance_criteria": [],
            "depends_on": [],
        }
        rendered = render_work_md([evil_item])

        self.assertEqual(
            rendered.count("## Proposed Work Items"),
            1,
            "an item's summary must not be able to inject a second "
            "'## Proposed Work Items' heading into the rendered board",
        )
        self.assertNotIn("### spoofed-item", rendered)

    def test_render_is_pure_and_order_preserving(self) -> None:
        items = [OPEN_ITEM, DEPENDENT_ITEM, PROPOSED_ITEM]
        first = render_work_md(items)
        second = render_work_md(items)
        self.assertEqual(first, second)
        # Order within a section follows graph order.
        self.assertLess(first.index("### health"), first.index("### metrics"))


class RegenerationTests(unittest.TestCase):
    def test_save_work_graph_writes_the_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            repository.mkdir()

            save_work_graph([OPEN_ITEM, PROPOSED_ITEM], repository)

            mirror = work_md_path(repository)
            self.assertTrue(mirror.is_file())
            self.assertEqual(
                mirror.read_text(encoding="utf-8"),
                render_work_md([OPEN_ITEM, PROPOSED_ITEM]),
            )

    def test_regeneration_is_idempotent_and_produces_no_diff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_repo(repository)

            # First write, then commit the generated mirror.
            save_work_graph([OPEN_ITEM, DEPENDENT_ITEM], repository)
            subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-m", "graph"],
                cwd=repository,
                check=True,
                capture_output=True,
            )

            # Re-saving the identical graph regenerates identical bytes.
            save_work_graph([OPEN_ITEM, DEPENDENT_ITEM], repository)
            status = subprocess.run(
                ["git", "status", "--porcelain", WORK_MD_FILENAME],
                cwd=repository,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(status.stdout, "", "an up-to-date mirror must not diff")

    def test_graph_mutating_command_regenerates_the_mirror(self) -> None:
        # `work ingest` is a graph-mutating command; running it in an isolated
        # temporary repository must refresh the WORK.md mirror.
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir) / "repo"
            _init_repo(repository)
            proposals = repository / ".agentflow" / "proposals"
            proposals.mkdir(parents=True)
            (proposals / "idea.json").write_text(
                json.dumps(
                    {
                        "kind": "new-work",
                        "summary": "Add a readiness probe",
                        "acceptance_criteria": ["GET /ready returns 200"],
                    }
                ),
                encoding="utf-8",
            )

            result = run_agentflow(
                "work", "ingest", "--repository", str(repository), cwd=repository
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            mirror = work_md_path(repository)
            self.assertTrue(mirror.is_file())
            text = mirror.read_text(encoding="utf-8")
            # The ingested proposal lands as a proposed item on the board.
            self.assertIn("Add a readiness probe", text)
            self.assertIn("## Proposed Work Items", text)

    def test_in_memory_backend_writes_no_mirror(self) -> None:
        # An in-memory backend has no repository, so save must not write a
        # mirror anywhere — the isolation that keeps this repo untouched in tests.
        backend = InMemoryWorkGraphBackend()
        cwd_before = set(Path.cwd().iterdir())
        save_work_graph([OPEN_ITEM], backend=backend)
        self.assertEqual(cwd_before, set(Path.cwd().iterdir()))
        self.assertEqual(backend.read_items(), [OPEN_ITEM])


class WriteHelperTests(unittest.TestCase):
    def test_write_work_md_returns_root_path_and_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            path = write_work_md(repository, [OPEN_ITEM])
            self.assertEqual(path, repository / WORK_MD_FILENAME)
            self.assertEqual(
                path.read_text(encoding="utf-8"), render_work_md([OPEN_ITEM])
            )


if __name__ == "__main__":
    unittest.main()
