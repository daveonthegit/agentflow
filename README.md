# Agentflow

Agentflow is a reusable, project-agnostic engineering workflow engine. Code
owns workflow state, evidence, Git isolation, checks, and approval gates. Model
adapters provide bounded planning, implementation, and review judgments.

See [`docs/README.md`](docs/README.md) for the architecture, domain decisions,
dogfooding threshold, and dependency-ordered roadmap.

## Run the test

```bash
python3 -m unittest discover -s tests -v
```

## Install Agentflow

After the repository is public, install the complete system with one command:

```bash
npx --yes github:daveonthegit/agentflow install
```

The bootstrapper:

1. Clones the full repository to `~/.local/share/agentflow/source`.
2. Creates an isolated Python environment.
3. Installs the CLI from the clone in editable mode.
4. Exposes `agentflow` through `~/.local/bin`.
5. Installs only the `agentflow` AI skill globally using the standard Skills
   CLI.

Rerun the same command to fast-forward the dedicated clone and reinstall the
CLI and skill. It refuses to overwrite a non-symlink command at the target
path.

## Contributor CLI installation

During development, install Agentflow into a dedicated environment and expose
its command from a directory on your `PATH`:

```bash
python3 -m venv ~/.local/share/agentflow/venv
~/.local/share/agentflow/venv/bin/python -m pip install --upgrade pip
~/.local/share/agentflow/venv/bin/python -m pip install --editable .
mkdir -p ~/.local/bin
ln -s ~/.local/share/agentflow/venv/bin/agentflow ~/.local/bin/agentflow
```

The editable install means changes in this checkout become available through
the command without reinstalling the package.

## Skill-only installation

The skill and CLI are separate: the skill teaches compatible AI coding agents
how to use Agentflow, while the CLI owns workflow state and verification.

Install only the Agentflow skill globally:

```bash
npx skills add git@github.com:daveonthegit/agentflow.git --skill agentflow -g
```

For a specific agent and a non-interactive install, add its agent identifier:

```bash
npx skills add git@github.com:daveonthegit/agentflow.git --skill agentflow -g -a codex -y
```

After the repository is public, the shorter equivalent is:

```bash
npx skills add daveonthegit/agentflow --skill agentflow -g
```

Then initialize any target repository from its root:

```bash
cd /path/to/your-project
agentflow init
```

`init` preserves existing repository instructions and installs a project-local
Agentflow skill at `.agents/skills/agentflow/SKILL.md`. This is the entry point
that tells compatible AI coding agents how to invoke the deterministic CLI.
Commit the profile and start from a clean checkout so the captured base commit
and profiled source are identical.

## Run the workflow

```bash
cd /path/to/your-project
agentflow init
agentflow profile --check "your formatter command" --check "your test command"
git add .agentflow/repository-profile.json
git commit -m "Add Agentflow repository profile"
agentflow start "Add a health endpoint"
```

The command snapshots the Task Spec, repository path, and exact base commit;
creates a unique Git branch and external worktree; and returns a run identifier
in the `ready` state.

Advance each stage from any process. Planning, building, and review need an
adapter; authoritative verification does not:

```bash
agentflow advance <run-id> --adapter codex  # ready -> planned
agentflow advance <run-id> --adapter codex  # planned -> built
agentflow advance <run-id>                  # built -> verified or failed
agentflow advance <run-id> --adapter codex  # verified -> awaiting_human
```

Inspect the replayed state at any point:

```bash
agentflow status <run-id>
```

When review records `awaiting_human`, inspect the Workspace diff and candidate
SHA. Approval must be an explicit command with a human identity:

```bash
agentflow approve <run-id> --approved-by <identity>
```

`agentflow run examples/task.json` remains as a compatibility command for
importing a JSON Task Spec into the same real kernel. It does not fabricate
planning, testing, or approval evidence.

Run Evidence and worktrees default to the platform's application-data
directory. On macOS this is `~/Library/Application Support/Agentflow`. Override
the location with `AGENTFLOW_HOME` or `--data-dir` for CI and isolated testing.

The append-only event history is stored at:

```text
<Agentflow Home>/runs/<run-id>/events.jsonl
```

## Current contract

The kernel owns run identity, immutable input snapshots, Repository Profile
integrity, Git worktree isolation, schema validation, allowed paths,
authoritative checks, append-only events, state replay, and approval bound to an
exact candidate SHA. Codex and deterministic fake adapters support planner,
builder, and reviewer roles. Tester, bounded repair, merge, post-merge
verification, and deployment are later slices.
