# Agentflow

Agentflow is a reusable, project-agnostic engineering workflow engine. Its goal
is to make model-produced changes **trustworthy** — to make it cheap to prove a
change is good, not expensive to produce it. Code owns workflow state, evidence,
Git isolation, checks, and approval gates; model adapters provide only bounded,
schema-validated judgments that no self-report can use to override that
evidence. Agentflow's identity is the gate: an approval bound to an exact,
verified revision.

Work moves through two halves. **Framing** — deciding what to build — is
interactive and warm, runs in the operator's main session, and ends when a human
approves a Work Graph. **Execution** — building, validating, and shipping an
approved Work Item — is cold, deterministic, and gated, with the Work Graph as
the only safe source of parallelism. See
[`docs/architecture/product-contract.md`](docs/architecture/product-contract.md)
for the goal and the full contract, and
[`docs/adr/0005-framing-is-warm-and-in-session.md`](docs/adr/0005-framing-is-warm-and-in-session.md)
for why framing is not a cold stage.

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
agentflow profile --check "your formatter command" --check "your test command" \
  --test-path tests
git add .agentflow/repository-profile.json
git commit -m "Add Agentflow repository profile"
agentflow start "Add a health endpoint"
```

Declare `--test-path` for every directory or file the Tester Agent Role may
touch; the tester stage refuses to run against a profile that records no
`test_paths`.

The command snapshots the Task Spec, repository path, and exact base commit;
creates a unique Git branch and external worktree; and returns a run identifier
in the `ready` state.

Advance each stage from any process. Building, testing, and review
need an adapter; authoritative verification does not:

```bash
agentflow advance <run-id> --adapter codex  # ready -> built
agentflow advance <run-id>                  # built -> verified or failed
agentflow advance <run-id> --adapter codex  # verified -> tested or failed
agentflow advance <run-id> --adapter codex  # tested -> awaiting_human
```

The tester writes tests only under the declared `test_paths`; when it adds a
test the authoritative checks re-run against the new commit, so a failing test
ends the Run at `failed`.

Inspect the replayed state at any point:

```bash
agentflow status <run-id>
```

When review records `awaiting_human`, inspect the Workspace diff and candidate
SHA. Approval must be an explicit command with a human identity:

```bash
agentflow approve <run-id> --approved-by <identity>
```

Rejection is likewise an explicit, human-attributed command
(`agentflow reject <run-id> --rejected-by <identity> [--reason <text>]`),
allowed only from `awaiting_human`, where it appends terminal `human_rejected`
bound to the candidate SHA.

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
exact candidate SHA, and a constrained Merge Agent that merges a current
Approved Revision only after deterministic approval, repository-policy, and
protected-branch gates plus a clean-environment CI gate that re-runs the
candidate's committed checks at the exact merge SHA in a fresh isolated
checkout. After a merge, `agentflow verify-merge` runs Post-Merge
Verification: the authoritative checks against the exact merged commit in an
isolated checkout, recorded as Run Evidence. A failure stops further shipping
for the repository — every subsequent merge is refused with evidence — and
records a Recovery Proposal for human review; only an attributed
`agentflow resolve-merge` lifts the block, and Agentflow never executes a
recovery itself. Claude, Cursor, Codex, and deterministic fake adapters
support builder, tester, and reviewer roles; Claude and Cursor
additionally provide live transcripts and model routing.

After a passing Post-Merge Verification, `agentflow deploy` ships the exact
verified `merged_sha` through a Deployment Adapter declared in the Repository
Profile (`agentflow profile ... --deploy-adapter directory|command` plus its
config; absent configuration refuses deployment by default). Deterministic
gates require write-once merge evidence, passing post-merge verification of
that exact commit, and no unresolved shipping stop; the adapter receives the
revision identity and an isolated checkout of exactly that commit — never a
Run Workspace or the primary checkout. Every refusal is a recorded
`deployment_refused` event; a completed deployment records
`deployment_completed` plus write-once `deployment.json` evidence, and neither
event changes the Run's workflow state or approval authority.
