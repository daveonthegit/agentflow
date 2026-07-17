"""``agentflow init``: configure a target repository for AI-assisted work.

Init installs three passive layers, none of which requires a running Agent or a
network:

* the project-local ``agentflow`` skill (copied verbatim);
* an always-on conventions section in the repository's agent instructions
  (``AGENTS.md``) that every agent reads while gathering context; and
* self-contained, dependency-free git hooks plus a committed enforcement
  policy that keeps the Work Graph hand-edit-proof and nudges commits toward
  carrying a ``Work-Item`` trailer.

The conventions and hooks are *passive*: in the default ``observe`` mode a
developer with no knowledge of Agentflow is never blocked by anything except a
direct edit to ``.agentflow/work/`` (the one thing that must go through
Agentflow). ``strict`` mode additionally requires a ``Work-Item`` trailer on
every commit.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess


AGENTFLOW_BLOCK_START = "<!-- agentflow:start -->"
AGENTFLOW_BLOCK_END = "<!-- agentflow:end -->"


AGENTFLOW_INSTRUCTIONS = """<!-- agentflow:start -->
## Agentflow

When the user explicitly asks to use Agentflow, follow the project-local
`agentflow` skill. Do not bypass its verification or human-approval gates.

### Working conventions (always apply)

These conventions apply to all work in this repository, whether or not you are
running Agentflow:

- **Consult `WORK.md` before starting.** It mirrors the open Work Items; pick
  the one you intend to deliver before you write code.
- **Carry the Work-Item id in your commits.** Add a `Work-Item: <id>` trailer
  so the work you land is matched to its open item.
- **Propose first for uncovered work.** If what you need to do is not an open
  Work Item, drop a JSON proposal into `.agentflow/proposals/`; a human ingests
  proposals into the Work Graph during Framing.
- **Never edit `.agentflow/work/` directly.** The Work Graph is mutated only
  through Agentflow; direct edits are rejected.
<!-- agentflow:end -->
"""


POLICY_RELATIVE = ".agentflow/policy.json"
DEFAULT_ENFORCEMENT = "observe"
ENFORCEMENT_MODES = ("observe", "strict")
POLICY_SCHEMA_VERSION = 1

MANAGED_HOOK_MARKER = "agentflow-managed-hook"
MANAGED_HOOKS = ("pre-commit", "commit-msg")


class PolicyNotCommittableError(RuntimeError):
    """The committed enforcement policy cannot be committed under git's ignores.

    Raised when the target repository's ignore rules would prevent
    ``.agentflow/policy.json`` from being committed. The enforcement mode is
    committed repository policy, so an un-committable policy path is a
    configuration error the developer must fix — init reports the exact rule to
    change rather than trying to defeat the ignore configuration.
    """

    def __init__(
        self,
        policy_relative: str,
        ignore_rule: str,
        fix_file: str,
        fix_lines: tuple[str, ...],
    ) -> None:
        self.policy_relative = policy_relative
        self.ignore_rule = ignore_rule
        self.fix_file = fix_file
        self.fix_lines = fix_lines
        rendered_fix = "".join(f"    {line}\n" for line in fix_lines)
        super().__init__(
            "agentflow init cannot proceed: the committed enforcement policy "
            f"'{policy_relative}' is ignored by git and could not be committed.\n"
            f"Matching ignore rule: {ignore_rule}\n"
            "The enforcement mode is committed repository policy, so this path "
            "must be committable. A file inside an ignored directory cannot be "
            "re-included by negating the file alone, so append these lines to "
            f"'{fix_file}':\n"
            f"{rendered_fix}"
            "then re-run `agentflow init`."
        )


@dataclass(frozen=True)
class InitResult:
    repository: Path
    enforcement: str
    policy_relative: str
    hooks_installed: tuple[str, ...]
    hooks_preserved: tuple[str, ...]


def initialize_repository(
    repository: Path, enforcement: str | None = None
) -> InitResult:
    if enforcement is not None and enforcement not in ENFORCEMENT_MODES:
        raise ValueError(f"unknown enforcement mode: {enforcement!r}")

    repository = repository.resolve()
    in_git = _is_git_work_tree(repository)

    # Prove committability before writing anything: if the policy path is
    # ignored, fail fast with the offending rule instead of leaving artifacts.
    if in_git:
        ignore_rule = _policy_ignore_rule(repository)
        if ignore_rule is not None:
            fix_file, fix_lines = _suggested_reinclusion(ignore_rule)
            raise PolicyNotCommittableError(
                POLICY_RELATIVE, ignore_rule, fix_file, fix_lines
            )

    _install_skill(repository)
    _write_instructions(repository)

    mode = _resolve_enforcement(repository, enforcement)
    _write_policy(repository, mode)

    installed: tuple[str, ...] = ()
    preserved: tuple[str, ...] = ()
    if in_git:
        installed, preserved = _install_hooks(repository)

    return InitResult(
        repository=repository,
        enforcement=mode,
        policy_relative=POLICY_RELATIVE,
        hooks_installed=installed,
        hooks_preserved=preserved,
    )


def _install_skill(repository: Path) -> None:
    source_skill = Path(__file__).parents[2] / "skills" / "agentflow"
    target_skill = repository / ".agents" / "skills" / "agentflow"
    shutil.copytree(source_skill, target_skill, dirs_exist_ok=True)


def _write_instructions(repository: Path) -> None:
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


def _resolve_enforcement(repository: Path, requested: str | None) -> str:
    """The mode to record: an explicit request wins, else keep any existing mode.

    Re-running init without ``--enforcement`` preserves a mode a maintainer
    already chose (so a strict repository stays strict), defaulting to
    ``observe`` only on the first run.
    """
    if requested is not None:
        return requested
    policy_path = repository / POLICY_RELATIVE
    if policy_path.exists():
        try:
            existing = json.loads(policy_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            existing = None
        if isinstance(existing, dict) and existing.get("enforcement") in ENFORCEMENT_MODES:
            return existing["enforcement"]
    return DEFAULT_ENFORCEMENT


def _write_policy(repository: Path, mode: str) -> Path:
    policy_path = repository / POLICY_RELATIVE
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"enforcement": mode, "schema_version": POLICY_SCHEMA_VERSION}
    policy_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return policy_path


def _install_hooks(repository: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Copy the vendored hooks into the repository's git hooks directory.

    A hook we manage (or an absent one) is (re)installed; a foreign hook of the
    same name is preserved untouched and reported, so init never clobbers a
    developer's own hook.
    """
    hooks_source = Path(__file__).parent / "hooks"
    hooks_dir = _hooks_dir(repository)
    hooks_dir.mkdir(parents=True, exist_ok=True)

    installed: list[str] = []
    preserved: list[str] = []
    for name in MANAGED_HOOKS:
        target = hooks_dir / name
        if target.exists():
            current = target.read_text(encoding="utf-8", errors="replace")
            if MANAGED_HOOK_MARKER not in current:
                preserved.append(name)
                continue
        shutil.copyfile(hooks_source / name, target)
        target.chmod(0o755)
        installed.append(name)
    return tuple(installed), tuple(preserved)


def _is_git_work_tree(repository: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _policy_ignore_rule(repository: Path) -> str | None:
    """Return the git ignore rule that would exclude the policy path, or None.

    Committability is decided by plain ``git check-ignore`` (exit 0 => ignored,
    1 => not): it excludes paths already tracked in the index and correctly
    treats a negation re-include as not ignored, whatever ignore form applies
    (glob, directory, or nested). Only when the path is genuinely ignored do we
    ask ``-v`` for the offending rule — ``-v`` reports the last matching pattern
    even a negation, so its exit code cannot be trusted to decide the question.
    """
    ignored = subprocess.run(
        ["git", "check-ignore", "--", POLICY_RELATIVE],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    if ignored.returncode != 0:
        return None
    verbose = subprocess.run(
        ["git", "check-ignore", "-v", "--", POLICY_RELATIVE],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    rule = verbose.stdout.strip()
    return rule or POLICY_RELATIVE


def _suggested_reinclusion(ignore_rule: str) -> tuple[str, tuple[str, ...]]:
    """Return the file to edit and the negation lines that re-include the policy.

    ``git check-ignore -v`` reports ``<source>:<line>:<pattern>\\t<pathname>``.
    Negation lives in ``<source>`` and is interpreted relative to that file's
    directory (except ``.git/info/exclude`` and the global excludes file, which
    are repo-root relative). Because git never descends into an excluded
    directory, re-including the policy file alone is not enough when a *parent*
    directory is excluded (the directory-ignore form): each ancestor directory
    between the source and the policy must be re-included first, then the file.
    Emitting the ancestor re-includes unconditionally is correct for every form
    — the extra lines are harmless no-ops when only the file (glob form) matches.
    """
    before_path = ignore_rule.split("\t", 1)[0]
    source = before_path.split(":", 2)[0] if ":" in before_path else ".gitignore"

    if PurePosixPath(source).name == ".gitignore":
        source_dir = str(PurePosixPath(source).parent)
    else:
        # info/exclude and core.excludesFile patterns are repo-root relative.
        source_dir = "."

    policy = PurePosixPath(POLICY_RELATIVE)
    if source_dir in ("", "."):
        relative = policy
    else:
        relative = policy.relative_to(source_dir)

    lines: list[str] = []
    ancestor = PurePosixPath("")
    for part in relative.parts[:-1]:
        ancestor = ancestor / part
        lines.append(f"!{ancestor.as_posix()}/")
    lines.append(f"!{relative.as_posix()}")

    fix_file = source if source else ".gitignore"
    return fix_file, tuple(lines)


def _hooks_dir(repository: Path) -> Path:
    raw = subprocess.run(
        ["git", "rev-parse", "--git-path", "hooks"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    hooks_dir = Path(raw)
    if not hooks_dir.is_absolute():
        hooks_dir = (repository / hooks_dir).resolve()
    return hooks_dir
