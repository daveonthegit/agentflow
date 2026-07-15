# Deterministic run kernel

The run kernel captures a task, Repository Profile, and exact repository
revision; creates an isolated Workspace; coordinates bounded Agent Roles;
executes authoritative checks; records ordered Run Evidence; and reconstructs
Run State in a later process. It stops before approval, merge, or shipping
unless those gates are explicitly satisfied.

## Public commands

```bash
agentflow init
agentflow profile --check "<command>"
agentflow start "<task summary>"
agentflow run <task.json>
agentflow advance <run-id> [--adapter codex]
agentflow status <run-id>
agentflow approve <run-id> --approved-by <human identity>
```

- `init` installs the canonical project-local Agentflow skill and a managed
  `AGENTS.md` block without replacing existing project instructions.
- `start` captures a Task Spec, resolves the Target Repository and base commit,
  verifies and references its target-local profile, creates one branch and
  Workspace, records events, and returns `ready`.
- `profile` creates the target-owned repository map, check commands, and source
  fingerprint at `.agentflow/repository-profile.json`.
- `advance` selects the next stage from replayed state. Planner, builder, and
  reviewer stages require an adapter. The built-to-verified transition executes
  profile checks directly without a model.
- `run` imports a JSON Task Spec into the same kernel for compatibility.
- `status` replays events in sequence and combines the result with captured
  input metadata.
- `approve` appends an explicit approval only when replayed state is
  `awaiting_human`. Conversation text is not approval evidence.

## Agentflow Home

Run Evidence and Workspaces live outside both Agentflow and the Target
Repository. Resolution order is:

1. `--data-dir`
2. `AGENTFLOW_HOME`
3. Platform application-data location

The macOS default is `~/Library/Application Support/Agentflow`. CI and tests use
an override so they cannot contaminate a developer's real runs.

```text
<Agentflow Home>/
├── runs/
│   └── <run-id>/
│       ├── task.json
│       ├── repository.json
│       ├── profile.json
│       ├── plan.json
│       ├── build-report.json
│       ├── checks.json
│       ├── review.json
│       └── events.jsonl
└── worktrees/
    └── <run-id>/
```

## Event contract

New events contain a one-based `sequence` equal to their line number in
`events.jsonl`. State is projected from event type:

| Event | Resulting state |
| --- | --- |
| `run_created` | `created` |
| `workspace_ready` | `ready` |
| `plan_ready` | `planned` |
| `build_ready` | `built` |
| `checks_passed` | `verified` |
| `checks_failed` | `failed` |
| `review_blocked` | `changes_requested` |
| `review_ready` | `reviewed` |
| `awaiting_human` | `awaiting_human` |
| `human_approved` | `human_approved` |

`repository_snapshotted` and `repository_profile_captured` add evidence without
changing state. `review_ready` is immediately followed by `awaiting_human` in
the current workflow. Legacy events without sequence numbers remain readable,
but any sequence number that is present must match its line position.

## Module map

- `src/agentflow/__main__.py` — thin CLI adapter.
- `src/agentflow/run_kernel.py` — Run lifecycle interface and implementation.
- `src/agentflow/repository_profile.py` — target-local discovery and freshness.
- `src/agentflow/contracts.py` — strict role-output validators and schemas.
- `src/agentflow/agent_adapter.py` — provider boundary, Codex, and fake adapters.
- `src/agentflow/workflow.py` — state-driven stage orchestration and checks.
- `src/agentflow/paths.py` — Agentflow Home resolution.
- `src/agentflow/project_setup.py` — idempotent Target Repository setup.
- `skills/agentflow/` — canonical distributable skill.
- `.agents/skills/agentflow/` — Agentflow's own project-local copy.

## Kernel invariants

- A Run captures one Task Spec and one exact base commit.
- A Run starts only from a clean Target Repository checkout.
- Each Run receives a unique branch and Workspace.
- The Run records a fresh target-owned Repository Profile by reference, hash,
  and source fingerprint instead of copying project knowledge into Agentflow.
- Planner, builder, and reviewer outputs must satisfy strict role contracts.
- The builder's authoritative Git diff must be a subset of planned paths and
  must equal its reported file list.
- Authoritative checks execute outside model reasoning against the candidate
  SHA, must leave its Workspace clean, and their raw results become Run
  Evidence.
- Run State comes from event replay, not an independently edited status file.
- Approval requires an explicit command, identity, clean Workspace, and exact
  candidate SHA.
- No agent report can override command exit status or recorded evidence.
- Target Repository documentation never becomes Agentflow documentation.

## Known limitations

- Repository discovery is intentionally shallow and does not yet infer entry
  points, architecture, or repository-specific domain language.
- Only the Codex and deterministic fake provider adapters exist.
- No tester, bounded builder-fix loop, merger, or deployment adapter exists.
- Worktree cleanup and abandoned-run recovery are not implemented.
- `advance` performs one stage per invocation; explicit plan approval and
  configurable role policies are not implemented yet.
