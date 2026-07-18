"""Prove a repo-tracked evidence path is committable under git's ignore rules.

Agentflow writes several files into the Target Repository whose whole value is
that they travel with the repository: the enforcement policy, the Work Graph
approval mirror (``.agentflow/approvals.jsonl``), external-completion
confirmations (``.agentflow/external-completions.jsonl``), and the proposals
inbox (``.agentflow/proposals/``). A repository whose ignore rules match one of
these paths silently swallows it -- the write appears to succeed locally but the
evidence never reaches a commit, so it is never shared. That failure is
invisible at write time and only surfaces as missing history much later.

This module decides committability with plain ``git check-ignore`` and, when a
path is ignored, raises an actionable error that names the offending rule and
the exact re-inclusion lines to add. It is the single mechanism behind the
fail-actionably behaviour ``agentflow init`` uses for ``policy.json``, reused so
every repo-tracked evidence writer proves its own path before it writes.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import subprocess


class EvidenceNotCommittableError(RuntimeError):
    """A repo-tracked evidence path is git-ignored and could not be committed.

    Raised when a Target Repository's ignore rules would prevent a file
    Agentflow relies on being shared through the repository (the enforcement
    policy, an approval mirror, external-completion evidence, or the proposals
    inbox) from being committed. The evidence is committed repository state, so
    an un-committable path is a configuration error the developer must fix: the
    message reports the exact offending rule and the re-inclusion lines to add
    rather than trying to defeat the ignore configuration.
    """

    def __init__(
        self,
        *,
        evidence_path: str,
        description: str,
        ignore_rule: str,
        fix_file: str,
        fix_lines: tuple[str, ...],
        retry_command: str,
    ) -> None:
        self.evidence_path = evidence_path
        self.description = description
        self.ignore_rule = ignore_rule
        self.fix_file = fix_file
        self.fix_lines = fix_lines
        self.retry_command = retry_command
        rendered_fix = "".join(f"    {line}\n" for line in fix_lines)
        super().__init__(
            f"agentflow cannot proceed: {description} "
            f"'{evidence_path}' is ignored by git and could not be committed.\n"
            f"Matching ignore rule: {ignore_rule}\n"
            "This path is repository-tracked evidence and must be committable. A "
            "file inside an ignored directory cannot be re-included by negating "
            "the file alone, so append these lines to "
            f"'{fix_file}':\n"
            f"{rendered_fix}"
            f"then re-run `{retry_command}`."
        )


def ensure_committable(
    repository: Path,
    evidence_relative: str,
    *,
    description: str,
    retry_command: str,
) -> None:
    """Raise if ``evidence_relative`` is git-ignored in ``repository``.

    ``evidence_relative`` is a repo-root-relative path (a file or a directory).
    Committability is decided by plain ``git check-ignore``; a repository that
    does not ignore the path -- including one that is not a git work tree at
    all, where there are no ignore rules to swallow anything -- is a silent
    no-op. Only when the path is genuinely ignored is an actionable
    :class:`EvidenceNotCommittableError` raised so the caller can fail before it
    writes evidence that would never be shared.
    """
    evidence_relative = PurePosixPath(evidence_relative).as_posix()
    ignore_rule = _ignore_rule(repository, evidence_relative)
    if ignore_rule is None:
        return
    fix_file, fix_lines = _suggested_reinclusion(evidence_relative, ignore_rule)
    raise EvidenceNotCommittableError(
        evidence_path=evidence_relative,
        description=description,
        ignore_rule=ignore_rule,
        fix_file=fix_file,
        fix_lines=fix_lines,
        retry_command=retry_command,
    )


def _ignore_rule(repository: Path, evidence_relative: str) -> str | None:
    """Return the git ignore rule that would exclude the path, or None.

    Committability is decided by plain ``git check-ignore`` (exit 0 => ignored,
    1 => not, other => not a work tree / error): it excludes paths already
    tracked in the index and correctly treats a negation re-include as not
    ignored, whatever ignore form applies (glob, directory, or nested). Only
    when the path is genuinely ignored do we ask ``-v`` for the offending rule
    -- ``-v`` reports the last matching pattern even a negation, so its exit
    code cannot be trusted to decide the question.
    """
    ignored = subprocess.run(
        ["git", "check-ignore", "--", evidence_relative],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    if ignored.returncode != 0:
        return None
    verbose = subprocess.run(
        ["git", "check-ignore", "-v", "--", evidence_relative],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    rule = verbose.stdout.strip()
    return rule or evidence_relative


def _suggested_reinclusion(
    evidence_relative: str, ignore_rule: str
) -> tuple[str, tuple[str, ...]]:
    """Return the file to edit and the negation lines that re-include the path.

    ``git check-ignore -v`` reports ``<source>:<line>:<pattern>\\t<pathname>``.
    Negation lives in ``<source>`` and is interpreted relative to that file's
    directory (except ``.git/info/exclude`` and the global excludes file, which
    are repo-root relative). Because git never descends into an excluded
    directory, re-including the evidence path alone is not enough when a
    *parent* directory is excluded (the directory-ignore form): each ancestor
    directory between the source and the path must be re-included first, then
    the path itself. Emitting the ancestor re-includes unconditionally is
    correct for every form -- the extra lines are harmless no-ops when only the
    path (glob form) matches.
    """
    before_path = ignore_rule.split("\t", 1)[0]
    source = before_path.split(":", 2)[0] if ":" in before_path else ".gitignore"

    if PurePosixPath(source).name == ".gitignore":
        source_dir = str(PurePosixPath(source).parent)
    else:
        # info/exclude and core.excludesFile patterns are repo-root relative.
        source_dir = "."

    evidence = PurePosixPath(evidence_relative)
    if source_dir in ("", "."):
        relative = evidence
    else:
        relative = evidence.relative_to(source_dir)

    lines: list[str] = []
    ancestor = PurePosixPath("")
    for part in relative.parts[:-1]:
        ancestor = ancestor / part
        lines.append(f"!{ancestor.as_posix()}/")
    lines.append(f"!{relative.as_posix()}")

    fix_file = source if source else ".gitignore"
    return fix_file, tuple(lines)
