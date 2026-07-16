# Deterministic run kernel

The run kernel captures a task, Repository Profile, and exact repository
revision; creates an isolated Workspace; coordinates bounded Agent Roles;
executes authoritative checks; records ordered Run Evidence; and reconstructs
Run State in a later process. It stops before approval, merge, or shipping
unless those gates are explicitly satisfied.

## Public commands

```bash
agentflow init
agentflow profile --check "<command>" [--test-path <repo-relative path>]
agentflow start ("<task summary>" | --work-item <id>)
agentflow run <task.json>
agentflow advance <run-id> [--adapter claude|cursor|codex] [--model <model>] [--claim-lease-seconds <seconds>]
agentflow models [--adapter claude|cursor --set <role>=<model>]
agentflow status <run-id>
agentflow watch <run-id>
agentflow list [--state <state>]
agentflow approve <run-id> --approved-by <human identity>
agentflow reject <run-id> --rejected-by <human identity> [--reason <text>]
agentflow abandon <run-id> --abandoned-by <identity> [--reason <text>]
agentflow rebase <run-id>
```

- `init` installs the canonical project-local Agentflow skill and a managed
  `AGENTS.md` block without replacing existing project instructions.
- `start` captures a Task Spec, resolves the Target Repository and base commit,
  verifies and references its target-local profile, creates one branch and
  Workspace, records events, and returns `ready`. New Runs always persist
  `acceptance_criteria` (empty is valid). Optional `source` is omitted for
  direct human starts unless supplied by imported task JSON. Repeatable
  `--acceptance-criterion` flags populate criteria on `start`. Alternatively,
  `start --work-item <id>` captures a Work Item from the Target Repository's Work
  Graph: its summary and acceptance criteria become the Task Spec and its
  `source` records provider `work-graph`, the `work_item_id`, the capture time,
  and the item's content hash (an unknown id is an error, and the summary and
  criteria then come from the item, not the command line).
- `profile` creates the target-owned repository map, check commands, and source
  fingerprint at `.agentflow/repository-profile.json`. Repeatable optional
  `--test-path <repo-relative path>` flags record the directories or files the
  Tester Agent Role may modify: each value must be a non-empty, repository-relative
  path that does not escape the repository, and the values are normalized,
  de-duplicated, and stored sorted as `test_paths` (schema stays version 1).
  Regenerating the profile without `--test-path` records no `test_paths` — there
  is no carry-forward merge.
- `advance` selects the next stage from replayed state. From `ready` it invokes
  the builder against the Task Spec (summary and acceptance criteria) and commits
  the candidate directly — there is no separate planner stage. Builder, tester,
  and reviewer stages require an adapter. The built-to-verified transition executes
  profile checks directly without a model.
- From `verified`, `advance` runs the Tester Agent Role exactly once per candidate
  generation (there is no path back to `verified` without a new build, repair, or
  rebase, so the tester cannot loop). The tester reads `test_paths` from the
  Workspace's committed, integrity-checked profile — never from the Target
  Repository root — and fails deterministically when that profile declares no
  `test_paths`, directing the operator to regenerate the profile with
  `--test-path`, commit it, and start a new Run. The kernel (not the prompt)
  enforces the tester's bounds: after the invocation the tester report's
  `files_changed` must exactly equal the authoritative dirty-path set and every
  changed path must be at or under a declared test path, or the stage fails hard
  naming the offending paths and appends no state-advancing event. Let G be the
  candidate generation at invocation; the tester commit does not change G. If the
  tester wrote no files it appends `tests_ready` carrying the unchanged
  `candidate_sha` and a `checks_artifact` reference to the existing
  `checks-<G>.json` (checks are not re-run) and becomes `tested`. If it wrote
  tests it commits `Agentflow run <run-id> tests <G>`, re-runs the authoritative
  profile checks against the new candidate into `checks-<G>-post-tests.json`
  (never overwriting `checks-<G>.json`), and on pass appends `tests_ready` (new
  `candidate_sha`, post-tests `checks_artifact`) → `tested`, or on failure appends
  `tests_failed` (new `candidate_sha`, post-tests `checks_artifact`, and the
  tester findings) → `failed`, exactly like `checks_failed`. Tester findings are
  evidence only: a blocker finding without a failing test never changes state.
- From `tested`, `advance` runs the reviewer. It resolves the candidate SHA and
  check evidence from the latest `tests_ready` event (which always carries both),
  requires the Workspace to be clean at that tester-produced candidate, passes the
  tester findings into the review request, and on approval binds `awaiting_human`
  to that SHA. The Claude adapter invokes the
  `claude` CLI with `--output-format stream-json --verbose` (partial-message
  streaming is not requested) while keeping its `--json-schema` structured
  output, resolves each role's model exactly once per invocation, and appends
  every received stream line to `runs/<run-id>/<role>-transcript.jsonl` in
  Agentflow Home as it arrives so the transcript can be tailed live. The final
  `result` stream event still supplies the schema-validated structured output,
  with unchanged error semantics for nonzero exit, error subtype, and missing
  structured output. The fake and Codex adapters produce no transcript.
- The Cursor adapter invokes the `agent` CLI in headless `stream-json` mode,
  using `ask` mode for the read-only reviewer stage and `--force
  --sandbox enabled` for the writing builder and tester stages. Because the Cursor CLI has no documented
  schema-constrained output option, the adapter prompts for one JSON object,
  extracts a candidate object from result text that may include progress prose,
  validates it locally against the role contract, and permits at most two
  output attempts. It writes every stream line to the same role transcript,
  preceded by an `agentflow_adapter_attempt` marker for each attempt. Nonzero
  exits, missing result events, and non-success result subtypes fail
  immediately with available diagnostics.
- `watch` follows a Run live and read-only: it prints each new line appended to
  the Run's `events.jsonl` and to whichever `<role>-transcript.jsonl` is
  growing, projecting Run State each poll, and prints a final status line and
  exits once the Run reaches a state requiring external action
  (`awaiting_human`, `changes_requested`, `failed`, `abandoned`,
  `human_approved`, `plan_rejected`, or `human_rejected`). It exits promptly
  when that condition is already true and never creates or modifies any
  evidence file.
- `advance --model` pins the model for the single stage that invocation
  performs and is accepted with `--adapter claude` or `--adapter cursor`.
  Each routing adapter resolves a role's model in precedence order — explicit
  `--model`, then its `AGENTFLOW_<ADAPTER>_<ROLE>_MODEL` environment variable,
  then routing recorded in `models.json` in Agentflow Home, then the adapter's
  suggested default. Claude suggests `opus` for every role; Cursor suggests
  `claude-opus-4-8-thinking-high` for every role. The resolved model is passed
  to the provider CLI and stamped on the stage event from that same single
  resolution; the workflow never re-resolves it.
- From `changes_requested`, `advance` performs bounded repair: while fewer than
  `MAX_REPAIR_ATTEMPTS` (2) `repair_ready` events exist, it invokes the builder
  with the Task Spec, latest review, current candidate, and one-based
  repair attempt, requires a clean Workspace at that candidate, enforces the
  same self-consistency check as the initial build (reported `files_changed`
  equal to the authoritative diff, no unresolved issues), commits a new
  candidate, appends `repair_ready`, and re-enters `built` so checks and review
  rerun. After two repairs, the next repair attempt appends `repair_exhausted`
  and becomes `failed` without invoking a model. Build, check, review, and
  repair reports and transcripts are attempt-scoped so earlier evidence is
  never overwritten; legacy flat artifact paths remain replayable.
- `models` honors `--data-dir` and, with no arguments, prints a sorted JSON
  object mapping each model-routing adapter (`claude` and `cursor`) to its
  `recorded` routing from `models.json` (an empty object when nothing is
  recorded) and its `suggested` defaults. With `--adapter claude` or
  `--adapter cursor` and repeatable `--set role=model`, it validates role names
  against `builder`, `reviewer`, and `tester`, merges the choices into
  `models.json`, and prints the updated routing in the same shape.
  `models.json` stores the user's recorded preference only; per-invocation
  provenance lives in the event log.
- `run` imports a JSON Task Spec into the same kernel for compatibility. It
  validates the Task Spec (non-empty `summary`; trimmed unique non-empty
  `acceptance_criteria`; optional `source` with exactly `provider`,
  `work_item_id`, `captured_at` as ISO-8601 with an explicit timezone, and
  `content_hash` as 64 lowercase hex), rejects unknown fields, and preserves
  optional `source` and criteria. `content_hash` is an importer-supplied
  upstream source-content reference and is never recomputed from task.json.
  Legacy summary-only task.json files remain readable. Material upstream task
  change requires a new Run; there is no snapshot refresh mutation.
- `status` replays events in sequence and combines the result with captured
  input metadata. Its JSON includes `repository_profile_path` when a
  `repository_profile_captured` event supplies that relative path; runs without
  profile evidence retain the legacy response shape. `source` and
  `acceptance_criteria` appear only when present and
  non-empty so legacy response shapes stay compatible.
- `list` replays every Run in Agentflow Home and prints a JSON array sorted by
  each Run's first event, so ordering is deterministic across invocations. Each
  entry carries the `status` fields `run_id`, `state`, `base_sha`, `summary`,
  and `repository`, plus `candidate_sha` and `approved_sha` when present.
  List stays concise and does not include `source` or `acceptance_criteria`.
  `--state` filters to one state; a missing or empty runs directory prints an
  empty array.
- `approve` acquires the Run's stage claim, then re-reads state and re-verifies
  the Workspace is clean at the current candidate SHA under the claim before
  appending an explicit approval, only when replayed state is `awaiting_human`.
  The claim guard prevents a concurrent rebase from moving the candidate between
  the check and the append, which would otherwise bind approval to a stale SHA.
  Conversation text is not approval evidence.
- `reject` acquires the Run's stage claim and appends a terminal rejection:
  from `awaiting_human`, `human_rejected` bound to the candidate SHA. It
  requires `--rejected-by` and accepts optional `--reason`. Conversation text is
  never rejection evidence. Rejected Runs cannot advance, approve, abandon,
  rebase, or be rejected again.
- `abandon` acquires the Run's stage claim before appending the terminal
  `run_abandoned` event, so a Run actively claimed by a live process cannot be
  abandoned out from under it. The event records the required `--abandoned-by`
  identity and the optional `--reason`. Abandoning an already-abandoned,
  `human_approved`, `plan_rejected`, or `human_rejected` Run fails.
- `rebase` refreshes a committed candidate onto the Target Repository's current
  main head so a Run whose base has fallen behind can be mechanically refreshed
  instead of abandoned. It first performs a read-only up-to-date check *before*
  acquiring any claim: it resolves the Target Repository's current main head
  (`git rev-parse HEAD` in the recorded repository path) and, when the Run's
  replayed base already equals it, returns `rebased: false` with the unchanged
  `base_sha` and appends no event at all. Otherwise it acquires the Run's stage
  claim, releases it in a `finally` block, and re-validates the replayed state
  under the claim: it applies only to `built`, `verified`, `changes_requested`,
  and `awaiting_human` (states with a committed candidate) and fails clearly on
  `ready`, `planned`, `failed`, `abandoned`, and `human_approved`. Under the
  claim it requires a clean Workspace, captures the prior Workspace HEAD, and
  runs `git rebase <new main head>` inside the Workspace. On clean application
  it verifies the Workspace is clean and appends one `candidate_rebased` event
  recording `old_base_sha`, `new_base_sha`, `old_candidate_sha`, and
  `new_candidate_sha` (the rebased Workspace HEAD). On conflict it runs
  `git rebase --abort`, restores the prior Workspace HEAD, and exits nonzero
  with a conflict message, appending no events beyond claim bookkeeping and
  leaving state, base, candidate, and the Workspace unchanged. `rebase` operates
  only inside the Workspace: it never touches the Target Repository's primary
  checkout, never pushes, and never merges. Because `candidate_rebased` projects
  state `built`, the existing checks and reviewer stages re-run against the new
  candidate and approval re-binds to the new SHA through the `awaiting_human`
  gate.
- `work` reads the Target Repository's Work Graph (git-tracked JSONL under
  `.agentflow/work/`, `--repository` defaulting to the current directory).
  `work list` validates and prints the whole graph; `work ready` prints the Work
  Items that are not yet complete and whose dependencies all are, deriving
  completion from `human_approved` Runs in Agentflow Home. It is strictly
  read-only over both the graph and Run Evidence and never writes state.

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
│       ├── build-report-<n>.json
│       ├── checks-<n>.json
│       ├── checks-<n>-post-tests.json
│       ├── tester-report-<n>.json
│       ├── review-<n>.json
│       ├── repair-report-<n>.json
│       ├── <role>[-<n>]-transcript.jsonl
│       └── events.jsonl
└── worktrees/
    └── <run-id>/
```

## Event contract

New events contain a one-based `sequence` equal to their line number in
`events.jsonl`. The append that assigns each `sequence` computes it and writes
the record under an exclusive advisory file lock (the same lock the claim
operations use), so concurrent writers to one Run's log are serialized and
sequence numbers stay contiguous — two racing appends can no longer collide on a
number and make the log permanently unreplayable. A stage-result append made by
a claim holder is refused if that holder no longer owns the active claim (for
example after another process took over an expired lease), so a stale worker
cannot overwrite the new holder's evidence. `list` isolates a single unreadable
Run as an `unreadable` entry rather than propagating the failure and hiding
every other Run. State is projected from event type:

| Event | Resulting state |
| --- | --- |
| `run_created` | `created` |
| `workspace_ready` | `ready` |
| `plan_ready` | `planned` |
| `build_ready` | `built` |
| `repair_ready` | `built` |
| `candidate_rebased` | `built` |
| `checks_passed` | `verified` |
| `checks_failed` | `failed` |
| `tests_ready` | `tested` |
| `tests_failed` | `failed` |
| `repair_exhausted` | `failed` |
| `review_blocked` | `changes_requested` |
| `review_ready` | `reviewed` |
| `awaiting_human` | `awaiting_human` |
| `human_approved` | `human_approved` |
| `plan_rejected` | `plan_rejected` |
| `human_rejected` | `human_rejected` |
| `run_abandoned` | `abandoned` |

`plan_ready`, `plan_rejected`, and `plan_amended` are legacy events that no
command emits now that the cold planner stage has been retired; their rows (and
`plan_amended` among the no-state-change events below) remain only so old run
logs continue to replay unchanged.

`build_ready`, `repair_ready`, `tests_ready`, `tests_failed`,
`review_ready`, and `review_blocked` carry a `model` field naming the resolved
model whenever the invoking adapter routes models (currently the Claude and Cursor
adapters); the deterministic fake and Codex adapters record no `model` field. The
same events also carry a `transcript` field naming the role transcript evidence
file whenever the
invoking adapter produced one (currently the Claude and Cursor adapters); the
fake and Codex adapters produce no transcript and therefore no `transcript`
field.
Both fields are provenance only — state projection is unchanged.
`repository_snapshotted`, `repository_profile_captured`, `claim_acquired`,
`claim_released`, `claim_expired`, and the legacy `plan_amended` add evidence
without changing state — none has an entry in the state-projection table above,
so the `state_by_event.get(type, state)` fallback leaves the replayed state
unchanged.
`review_ready` is immediately followed by `awaiting_human` in the current
workflow. `abandoned`, `plan_rejected`, and `human_rejected` are terminal: a
Run can never advance and can never be approved from them. Legacy events
without sequence numbers remain readable, but any sequence number that is
present must match its line position.
The Repository Profile path reported by `status` is replayed from
`repository_profile_captured`, rather than rediscovered from the current Target
Repository.
`candidate_rebased` carries `old_base_sha`, `new_base_sha`, `old_candidate_sha`,
and `new_candidate_sha`; it projects state `built`. When it is present, `status`
and `list` replay the latest rebased base (`new_base_sha`) as `base_sha` and the
rebased candidate (`new_candidate_sha`) as `candidate_sha`; runs without a rebase
event keep the `base_sha` recorded in `repository.json`. The checks stage
resolves its candidate SHA from the newest of `build_ready`, `repair_ready`, and
`candidate_rebased`, so checks re-run against the latest candidate. Attempt
numbers for build, check, review, and transcript artifacts advance for every
new candidate generation — `build_ready`, `repair_ready`, and
`candidate_rebased` alike — so post-rebase checks and reviews never overwrite
pre-rebase evidence. Legacy flat names (`plan.json`, `checks.json`,
`review.json`, `build-report.json`) remain readable when an event's `artifact`
field points at them.
`tests_ready` and `tests_failed` always carry both a `candidate_sha` and a
`checks_artifact` reference, and both are candidate-producing event types, so
downstream stages resolve the latest candidate from them after the tester runs.
Because the tester commit deliberately does not advance the candidate generation,
post-tests checks land in `checks-<G>-post-tests.json` for the same generation G
while the reviewer for that generation still writes `review-<G>.json`.

## Task Spec snapshot

`task.json` is an immutable Task Spec captured at Run start. **Implemented**
fields:

- `summary` — required non-empty string.
- `acceptance_criteria` — list of trimmed unique non-empty strings; new Runs
  always persist the field (empty is valid).
- `source` — optional object with exactly `provider`, `work_item_id`,
  `captured_at` (ISO-8601 with an explicit timezone), and `content_hash`
  (exactly 64 lowercase hexadecimal characters). The hash is an importer-supplied
  upstream source-content reference and is never recomputed from task.json.
  Direct human `start` omits `source` unless the Task Spec was imported.

Unknown Task Spec fields are rejected. Legacy summary-only `task.json` files
remain replayable. Material upstream task change requires a new Run; there is
no snapshot refresh mutation. The complete frozen task object is passed
unchanged to the builder, tester, and reviewer stages.

## Check evidence enrichment

Each individual check record inside attempt-scoped `checks-<n>.json` includes
**Implemented** enrichment fields:

- `started_at` — timezone-aware UTC ISO-8601 captured immediately before the
  check subprocess starts.
- `duration_ms` — non-negative integer measured with a monotonic clock.
- `attempt` — the one-based candidate generation already used for attempt-scoped
  artifacts; shared by every check in one stage and incremented after
  `build_ready`, `repair_ready`, or `candidate_rebased`.
- `environment` — a fixed allowlist only: `LANG`, `PYTHONHASHSEED`, `TZ`, plus
  Python implementation/version and OS system/release/machine. Arbitrary process
  environment variables and secrets are never recorded.

Every check in one stage shares the same `attempt` and `environment` fingerprint
but records its own `started_at` and `duration_ms`. Legacy `checks.json` records
without these fields remain readable.

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
- The default lease is `DEFAULT_CLAIM_LEASE_SECONDS = 14400`, which must strictly
  exceed the adapter subprocess timeout plus the authoritative check budget for a
  single stage: the tester stage runs an adapter invocation (3600-second timeout)
  and then re-runs the profile checks (1800-second per-command timeout) within one
  claim, so a live stage cannot lose its claim while its work is permitted to run.
  `advance --claim-lease-seconds` overrides the lease for one invocation.
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
- `src/agentflow/agent_adapter.py` — provider boundary and Claude, Cursor,
  Codex, and fake adapters.
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
- Builder, tester, and reviewer outputs must satisfy strict role
  contracts.
- The builder is confined by self-consistency: its reported `files_changed`
  must equal its authoritative Git diff and it must report no unresolved issues.
  There is no pre-declared file list or planned-path subset check.
- The tester may modify only files at or under the profile's declared
  `test_paths` and never production code; its authoritative Git diff must equal
  its reported file list, and its prose findings are evidence only — only failing
  tests the authoritative checks run change Run State.
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
- Rejection requires an explicit command and identity; conversation text is
  never rejection evidence. `human_rejected` is terminal (legacy `plan_rejected`
  remains terminal on replay).
- A candidate may be repaired from `changes_requested` at most
  `MAX_REPAIR_ATTEMPTS` times; each repair commits a new candidate, preserves
  prior attempt artifacts, and re-enters `built` for checks and review.
- A candidate may be rebased onto the Target Repository's advanced main only
  from a state with a committed candidate, only inside the Workspace, and never
  against the Target Repository's primary checkout; an up-to-date Run appends no
  event, and a conflicting rebase aborts and leaves state, base, candidate, and
  the Workspace unchanged. A rebased candidate re-enters `built` so it must pass
  checks, review, and the human gate again.
- No agent report can override command exit status or recorded evidence.
- Target Repository documentation never becomes Agentflow documentation.

## Known limitations

- Repository discovery is intentionally shallow and does not yet infer entry
  points, architecture, or repository-specific domain language.
- Only the Claude, Cursor, Codex, and deterministic fake provider adapters
  exist.
- The Claude adapter limits the reviewer role to read-only tools, but
  its builder and tester roles rely on role instructions and the kernel's
  diff enforcement (builder self-consistency, tester `test_paths`) rather than
  an operating-system sandbox.
- The Cursor CLI has no documented JSON Schema output flag or per-invocation
  granular tool allowlist. The Cursor adapter therefore uses read-only `ask`
  mode for the reviewer, requests the CLI sandbox for the builder and
  tester, and relies on prompt-plus-local-validation with a two-attempt bound for
  output contracts.
- The Tester Agent Role runs after authoritative checks pass and before review,
  writing tests only under the profile's declared `test_paths`. The repair loop
  from a `tests_failed` state is deferred: a failing tester test currently ends
  the Run at `failed`, exactly like `checks_failed`. No merger or deployment
  adapter exists.
- Worktree and Workspace cleanup is not implemented: `abandon` records the
  terminal state but leaves the Run's Workspace and branch on disk.
- `advance` performs one stage per invocation; configurable role policies are
  not implemented yet.
