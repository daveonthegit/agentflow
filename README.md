# Agentflow starter

This is the first tracer bullet of a reusable agentic engineering workflow engine. It intentionally contains no model SDK, Git integration, or remote services.

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

## Run the workflow

```bash
agentflow run examples/task.json --data-dir .agentflow
```

The command prints a run identifier and `awaiting_human`. Its append-only event history is written to:

```text
.agentflow/runs/<run-id>/events.jsonl
```

## Current contract

`run` validates that the task is JSON, creates a unique run, records fake workspace/plan/check events, and pauses for human approval. The next test will add `status`, which must rebuild the reported state by replaying the event history in a new process.
