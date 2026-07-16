from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import shlex
import subprocess


PROFILE_RELATIVE_PATH = Path(".agentflow/repository-profile.json")


@dataclass(frozen=True)
class CreatedProfile:
    path: Path
    source_fingerprint: str


@dataclass(frozen=True)
class ProfileEvidence:
    path: str
    source_fingerprint: str
    profile_sha256: str
    fresh: bool


def _repository_root(repository: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    )
    return Path(result.stdout.strip())


def _repository_files(repository: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repository,
        capture_output=True,
        check=True,
    )
    paths = [Path(value.decode("utf-8")) for value in result.stdout.split(b"\0") if value]
    return sorted(path for path in paths if path != PROFILE_RELATIVE_PATH)


def _source_fingerprint(repository: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for relative_path in files:
        digest.update(str(relative_path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256((repository / relative_path).read_bytes()).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def _validated_test_paths(test_paths: list[str]) -> list[str]:
    """Normalize declared test paths for the profile.

    Each value must be a non-empty, repository-relative path that does not
    escape the repository. Values are normalized, de-duplicated, and returned
    sorted so the profile is deterministic regardless of flag order.
    """
    validated: set[str] = set()
    for raw in test_paths:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("Repository Profile test paths must not be empty")
        candidate = PurePosixPath(raw.strip())
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(
                "Repository Profile test paths must be repository-relative "
                "and must not escape the repository"
            )
        validated.add(str(candidate))
    return sorted(validated)


def create_repository_profile(
    *, repository: Path, checks: list[str], test_paths: list[str] | None = None
) -> CreatedProfile:
    repository = _repository_root(repository)
    files = _repository_files(repository)
    source_fingerprint = _source_fingerprint(repository, files)
    top_level = sorted({path.parts[0] for path in files})
    documentation = sorted(str(path) for path in files if path.suffix == ".md")
    parsed_checks = [shlex.split(check) for check in checks]
    if not all(parsed_checks):
        raise ValueError("Repository Profile checks must not be empty")
    profile = {
        "checks": parsed_checks,
        "map": {
            "documentation": documentation,
            "top_level": top_level,
        },
        "schema_version": 1,
        "source_fingerprint": source_fingerprint,
    }
    if test_paths:
        profile["test_paths"] = _validated_test_paths(test_paths)
    profile_path = repository / PROFILE_RELATIVE_PATH
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        json.dumps(profile, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return CreatedProfile(path=profile_path, source_fingerprint=source_fingerprint)


def inspect_repository_profile(repository: Path) -> ProfileEvidence | None:
    repository = _repository_root(repository)
    profile_path = repository / PROFILE_RELATIVE_PATH
    if not profile_path.exists():
        return None
    profile_bytes = profile_path.read_bytes()
    profile = json.loads(profile_bytes)
    current_fingerprint = _source_fingerprint(
        repository,
        _repository_files(repository),
    )
    return ProfileEvidence(
        path=str(PROFILE_RELATIVE_PATH),
        source_fingerprint=profile["source_fingerprint"],
        profile_sha256=hashlib.sha256(profile_bytes).hexdigest(),
        fresh=profile["source_fingerprint"] == current_fingerprint,
    )
