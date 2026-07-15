from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil


AGENTFLOW_BLOCK_START = "<!-- agentflow:start -->"
AGENTFLOW_BLOCK_END = "<!-- agentflow:end -->"


AGENTFLOW_INSTRUCTIONS = """<!-- agentflow:start -->
## Agentflow

When the user explicitly asks to use Agentflow, follow the project-local
`agentflow` skill. Do not bypass its verification or human-approval gates.
<!-- agentflow:end -->
"""


@dataclass(frozen=True)
class InitResult:
    repository: Path


def initialize_repository(repository: Path) -> InitResult:
    repository = repository.resolve()

    source_skill = Path(__file__).parents[2] / "skills" / "agentflow"
    target_skill = repository / ".agents" / "skills" / "agentflow"
    shutil.copytree(source_skill, target_skill, dirs_exist_ok=True)

    agents_path = repository / "AGENTS.md"
    existing = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
    block_start = existing.find(AGENTFLOW_BLOCK_START)
    block_end = existing.find(AGENTFLOW_BLOCK_END)
    if block_start >= 0 and block_end >= block_start:
        block_end += len(AGENTFLOW_BLOCK_END)
        updated = (
            existing[:block_start]
            + AGENTFLOW_INSTRUCTIONS.rstrip("\n")
            + existing[block_end:]
        )
    else:
        separator = "" if not existing or existing.endswith("\n\n") else "\n"
        updated = existing + separator + AGENTFLOW_INSTRUCTIONS
    agents_path.write_text(updated, encoding="utf-8")

    return InitResult(repository=repository)
