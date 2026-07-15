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
agentflow advance <run-id> [--adapter claude|codex] [--model <model>] [--claim-lease-seconds <seconds>]
agentflow models [--adapter claude --set <role>=<model>]
agentflow status <run-id>
agentflow list [--state <state>]
agentflow approve <run-id> --approved-by <human identity>
agentflow abandon <run-id> --abandoned-by <identity> [--reason <text>]
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
- `advance --model` pins the model for the single stage that invocation
  performs and is accepted only with `--adapter claude`. The Claude adapter
  resolves each role's model in precedence order — explicit `--model`, then
  the `AGENTFLOW_CLAUDE_PLANNER_MODEL`, `AGENTFLOW_CLAUDE_BUILDER_MODEL`, or
  `AGENTFLOW_CLAUDE_REVIEWER_MODEL` environment variable, then the routing
  recorded in `models.json` in Agentflow Home, then the adapter's suggested
  defaults (`fable` for the planner, `opus` for the builder and reviewer) —
  and passes the resolved model to the `claude` CLI as `--model`.
- `models` honors `--data-dir` and, with no arguments, prints a sorted JSON
  object mapping each model-routing adapter (currently `claude`) to its
  `recorded` routing from `models.json` (an empty object when nothing is
  recorded) and its `suggested` defaults. With `--adapter claude` and
  repeatable `--set role=model` it validates role names against `planner`,
  `builder`, and `reviewer`, merges the choices into `models.json`, and prints
  the updated routing in the same shape. `models.json` stores the user's
  recorded preference only; per-invocation provenance lives in the event log.
- `run` imports a JSON Task Spec into the same kernel for compatibility.
- `status` replays events in sequence and combines the result with captured
  input metadata. Its JSON includes `repository_profile_path` when a
  `repository_profile_captured` event supplies that relative path; runs without
  profile evidence retain the legacy response shape.
- `list` replays every Run in Agentflow Home and prints a JSON array sorted by
  each Run's first event, so ordering is deterministic across invocations. Each
  entry carries the `status` fields `run_id`, `state`, `base_sha`, `summary`,
  and `repository`, plus `candidate_sha` and `approved_sha` when present.
  `--state` filters to one state; a missing or empty runs directory prints an
  empty array.
- `approve` appends an explicit approval only when replayed state is
  `awaiting_human`. Conversation text is not approval evidence.
- `abandon` acquires the Run's stage claim before appending the terminal
  `run_abandoned` event, so a Run actively claimed by a live process cannot be
  abandoned out from under it. The event records the required `--abandoned-by`
  identity and the optional `--reason`. Abandoning an already-abandoned or
  `human_approved` Run fails.

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
├── models.json
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
| `run_abandoned` | `abandoned` |

`plan_ready`, `build_ready`, `review_ready`, and `review_blocked` carry a
`model` field naming the resolved model whenever the invoking adapter routes
models (currently the Claude adapter); the deterministic fake adapter records
no `model` field. The field is provenance only — state projection is
unchanged.
`repository_snapshotted`, `repository_profile_captured`, `claim_acquired`,
`claim_released`, and `claim_expired` add evidence without changing state.
`review_ready` is immediately followed by `awaiting_human` in the current
workflow. `abandoned` is terminal: a Run can never advance and can never be
approved from it. Legacy events without sequence numbers remain readable, but
any sequence number that is present must match its line position.
The Repository Profile path reported by `status` is replayed from
`repository_profile_captured`, rather than rediscovered from the current Target
Repository.

## Stage claims

Every `advance` acquires an atomic per-Run stage claim before executing any
stage logic and releases it when the stage completes or fails. The event log is
the only claim authority: no lock file or second store of claim state exists.

- A claim is acquired by compare-and-append: the process opens `events.jsonl`,
  takes an exclusive advisory `flock` on that same file handle, replays the
  active claim from `claim_acquired`, `claim_released`, and `claim_expired`
  events, and appends `claim_acquired` only when no unexpired claim is active.
  All appends happen through the locked handle with the normal
  sequence-equals-line-number rule.
- `claim_acquired` carries the claiming process identity in `holder`
  (`hostname:pid` by default), an ISO-8601 UTC `acquired_at`, and an ISO-8601
  UTC lease expiry in `expires_at`. `claim_released` and `claim_expired` repeat
  the released or expired claim's `holder` and `expires_at`.
- A second `advance` on the same Run while an unexpired claim is active fails
  with an error naming the holder and expiry, and appends nothing.
- The default lease is `DEFAULT_CLAIM_LEASE_SECONDS = 7200`, which strictly
  exceeds the 3600-second adapter subprocess timeout so a live stage cannot
  lose its claim while its adapter is still permitted to run. `advance
  --claim-lease-seconds` overrides the lease for one invocation.
- An expired claim no longer blocks: the next `advance` appends
  `claim_expired` evidence and then acquires its own claim. A stage that
  legitimately exceeds its lease therefore relies on expired-claim recovery,
  not on a mutual-exclusion guarantee.
- Release is holder-scoped: `claim_released` is appended only when the active
  claim's holder equals the releasing process identity. A superseded or
  expired holder's release appends nothing, so it can never clear a claim now
  legitimately held by another process.
- The advisory `flock` is POSIX-only (`fcntl`), so the kernel currently
  requires a POSIX platform.

## Module map

- `src/agentflow/__main__.py` — thin CLI adapter.
- `src/agentflow/run_kernel.py` — Run lifecycle interface and implementation.
- `src/agentflow/repository_profile.py` — target-local discovery and freshness.
- `src/agentflow/contracts.py` — strict role-output validators and schemas.
- `src/agentflow/agent_adapter.py` — provider boundary, Claude, Codex, and fake
  adapters.
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
- Every `advance` acquires a stage claim before executing its stage; the event
  log is the only claim authority.
- Claim release is holder-scoped: only the process whose claim is active can
  release it, on completion or failure alike.
- Approval requires an explicit command, identity, clean Workspace, and exact
  candidate SHA.
- No agent report can override command exit status or recorded evidence.
- Target Repository documentation never becomes Agentflow documentation.

## Known limitations

- Repository discovery is intentionally shallow and does not yet infer entry
  points, architecture, or repository-specific domain language.
- Only the Claude, Codex, and deterministic fake provider adapters exist.
- The Claude adapter limits planner and reviewer roles to read-only tools, but
  its builder role relies on role instructions and the kernel's planned-path
  diff enforcement rather than an operating-system sandbox.
- No tester, bounded builder-fix loop, merger, or deployment adapter exists.
- Worktree and Workspace cleanup is not implemented: `abandon` records the
  terminal state but leaves the Run's Workspace and branch on disk.
- `advance` performs one stage per invocation; explicit plan approval and
  configurable role policies are not implemented yet.
