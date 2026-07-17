# Agentflow product and architecture contract

This is the canonical contract for the Agentflow factory: the agreed behavior
Agentflow must exhibit, with every statement classified as implemented, as
target, or as a confirmed decision. Domain terms are defined in [`../../CONTEXT.md`](../../CONTEXT.md);
kernel mechanics, event contracts, and evidence layout are detailed in
[`run-kernel.md`](run-kernel.md); unresolved implementation choices live in
[`../decisions/agentflow-factory.md`](../decisions/agentflow-factory.md). This
document does not duplicate those sources.

## Purpose

Agentflow exists to make model-produced changes **trustworthy**: to make it
cheap to *prove* a change is good, not expensive to *produce* it. Its identity
is the gate — an approval bound to an exact, verified revision, backed by
evidence no model self-report can override. When goals conflict on cost, trust
wins; consistent quality is enforced at the gate (what evidence must exist)
rather than by running every stage, and unattended autonomy is a later
multiplier that is only turned on once a trusted change is both cheap and sound.

Work moves through two halves with a deliberate seam between them:

- **Framing** — deciding *what* to build. Interactive, warm, human-in-the-loop,
  driven by an Agentflow-owned skill: clarify intent, surface edge cases,
  produce efficient documentation, and decompose the work into a Work Graph.
  Framing runs in the operator's main session, not as a cold stage, and ends
  when the human approves the Work Graph (content-hashed, immutable). See
  [ADR 0005](../adr/0005-framing-is-warm-and-in-session.md).
- **Execution** — turning an approved Work Item into shipped, validated code.
  Cold, deterministic, and gated: the Work Graph is the only safe source of
  parallelism, and independent Work Items run as separate gated Runs.

The Work Graph is the boundary object between the two halves. A cold Run never
invents its own decomposition or parallelism; it executes work a human already
approved. This document classifies where each half stands today.

## How to read this contract

Every behavior statement carries one of three classifications:

- **Implemented** — enforced by code in `src/` today and covered by the test
  suite. `run-kernel.md` describes the mechanism.
- **Target** — agreed direction that no code enforces yet. Target behavior
  must never be described as implemented; when a target item lands, its
  classification changes here and the mechanism is documented in the
  architecture docs.
- **Confirmed decision** — an agreed rule or mechanism choice that is binding
  on future work but is not itself a code-enforced behavior. A confirmed
  decision resolves what would otherwise be an open ticket in the decision
  map; the behavior it governs may still be classified target until code
  enforces it.

## Run lifecycle

- **Implemented.** A Run captures one immutable Task Spec and one exact base
  commit, starts only from a clean Target Repository checkout, and proceeds
  through builder, checks, tester, and reviewer stages driven by
  replayed state. `advance` performs one stage per invocation. The Task Spec may include
  optional `source` (`provider`, `work_item_id`, `captured_at` with an explicit
  timezone, and importer-supplied `content_hash`) and `acceptance_criteria`
  (trimmed unique non-empty strings). New Runs always store
  `acceptance_criteria` (empty is valid); `source` is omitted for direct human
  starts unless supplied by imported task JSON. Unknown Task Spec fields are
  rejected. Legacy summary-only task snapshots remain replayable. Material
  upstream task change requires a new Run — there is no snapshot refresh.
  `status` exposes `source` and `acceptance_criteria` only when present and
  non-empty; `list` stays concise without those fields. The complete frozen
  task object is passed to the builder, tester, and reviewer.
- **Implemented.** Builder, tester, and reviewer outputs must satisfy
  strict versioned role contracts; the builder is confined by self-consistency —
  its reported `files_changed` must equal its authoritative Git diff and it must
  report no unresolved issues — and the tester's
  authoritative Git diff must equal its reported file list and stay at or under
  the profile's declared `test_paths`.
- **Implemented.** Workspace integrity is enforced beyond the tracked-file diff:
  across every builder, tester, and reviewer invocation the git-hook state
  (contents of the resolved hooks directory plus the effective `core.hooksPath`)
  must be unchanged, and no ignored files may be introduced. This closes the
  escape in which a role plants a hook that would execute at the next
  in-Workspace commit, or leaves `.gitignore`d state that sways the authoritative
  checks without entering the committed candidate. The read-only reviewer
  additionally must leave Workspace HEAD and status unchanged.
- **Implemented.** A fresh process can replay a Run's events and continue from
  the recorded state.
- **Implemented.** Runs are enumerable: `list` projects every Run in Agentflow
  Home from replayed events, deterministically ordered by each Run's first
  event, with an optional state filter.
- **Implemented.** Explicit abandonment: `abandon` acquires the Run's stage
  claim, then appends a terminal `run_abandoned` event carrying the abandoning
  identity and an optional reason. An abandoned Run can never advance and can
  never be approved, and abandoning an already-abandoned or `human_approved`
  Run fails. Abandonment does not remove the Run's Workspace or worktree.
- **Implemented.** Candidate rebase with mandatory re-verification: `rebase`
  refreshes a Run whose base has fallen behind onto the Target Repository's
  current main head instead of forcing abandonment, enabling concurrent Runs. A
  read-only up-to-date check runs before any claim is acquired, so a Run already
  on main appends no event; otherwise the Run's stage claim guards the rebase,
  which applies only from a state with a committed candidate (`built`,
  `verified`, `changes_requested`, `awaiting_human`) and fails clearly on
  pre-candidate and terminal states. A clean rebase appends one
  `candidate_rebased` event that re-enters state `built`; a conflict aborts and
  leaves state, base, candidate, and the Workspace unchanged. The rebase runs
  only inside the Workspace and never touches the Target Repository's primary
  checkout, pushes, or merges. Approval invalidation on rebase is inherent:
  approval binds to the exact candidate SHA, and a rebase produces a new SHA
  that must pass checks, review, and the `awaiting_human` gate again.
- **Implemented.** Bounded repair out of `changes_requested` and `tests_failed`:
  while fewer than `MAX_REPAIR_ATTEMPTS` (2) `repair_ready` events exist,
  `advance` invokes the builder with the Task Spec, the current candidate, the
  one-based repair attempt, and the trigger-specific evidence (the latest review
  from `changes_requested`, or the failing post-tests checks and tester findings
  from `tests_failed`); commits a new candidate; appends `repair_ready`; and
  re-enters `built` so checks, the tester, and review rerun. The `repair_ready`
  budget is shared across both triggers. After two repairs, the next repair
  attempt appends terminal `repair_exhausted` without invoking a model. Each
  build, check, review, and repair writes a distinct attempt-scoped artifact so
  earlier evidence is never overwritten. Attempt numbering also advances on
  `candidate_rebased`, so checks and reviews after a rebase cannot overwrite
  pre-rebase artifacts.
- **Implemented.** Explicit rejection: claim-guarded `reject` with required
  `--rejected-by` and optional `--reason`. From `awaiting_human` it appends
  terminal `human_rejected` bound to the candidate SHA. Rejection conversation
  text is never evidence.
  Rejected Runs cannot advance, approve, abandon, rebase, or be rejected again.
- **Implemented.** Adversarial Tester Agent Role between checks and review: from
  `verified`, `advance` runs the tester exactly once per candidate generation. It
  may write only files at or under the profile's declared `test_paths` and never
  production code, enforced by the kernel against the authoritative Git diff. If
  it writes tests it commits a new candidate and re-runs the authoritative checks
  into a distinct `checks-<G>-post-tests.json`, reaching `tested` on pass and the
  non-terminal, repairable `tests_failed` on failure; if it writes nothing it
  reaches `tested` against the unchanged candidate without re-running checks. Its
  prose findings are recorded and surfaced to the reviewer but never gate the
  workflow on their own. A run lacking declared `test_paths` fails the stage
  deterministically.
- **Implemented.** A constrained Merge Agent merges a current Approved
  Revision only after deterministic approval and repository-policy gates, and
  Post-Merge Verification runs the authoritative checks against the exact
  merged commit in an isolated checkout, recording the result as Run
  Evidence. A failure stops further shipping for the Target Repository —
  every subsequent merge is refused with evidence — and records a
  human-reviewed Recovery Proposal; only an attributed resolution lifts the
  block, and Agentflow never executes a recovery itself.
- **Target.** Deployment remains manual after approval until deployment
  adapters exist.
- **Target.** Reconciliation and Workspace cleanup after abandonment.

## Evidence

- **Implemented.** Run Evidence is append-only, sequence-numbered, and stored
  in Agentflow Home, outside both the Agentflow repository and every Target
  Repository. Run State is projected from event replay, never from an
  independently mutable status file.
- **Implemented.** Authoritative checks execute outside model reasoning
  against the exact candidate SHA, must leave the Workspace clean, and their
  raw results become Run Evidence. No agent report can override command exit
  status or recorded evidence. Each individual check record includes
  `started_at` (UTC ISO-8601 immediately before subprocess execution),
  `duration_ms` (non-negative via monotonic clock), `attempt` (one-based
  candidate generation, shared per stage including after repair and rebase),
  and an allowlisted environment fingerprint only (`LANG`, `PYTHONHASHSEED`,
  `TZ`, Python implementation/version, OS system/release, machine) with no
  arbitrary environment secrets. Legacy check records without these fields
  remain readable.
- **Target.** Evidence-driven improvement: Improvement Proposals generated
  from repeated Run Evidence, evaluated against fixtures, and gated by an
  Adoption Gate.

## Approval

- **Implemented.** The workflow records `awaiting_human` only after checks
  pass and review does not block. Approval requires an explicit command, a
  human identity, a clean Workspace, and the exact Candidate Revision SHA.
  Conversation text is never approval evidence.
- **Implemented.** Any code change after approval invalidates it; the new
  revision must pass verification and approval again.
- **Implemented.** Approval-scoped merge automation: the Merge Agent acts only
  on a current Approved Revision after deterministic policy gates.

## Repository Profile boundary

- **Implemented.** Each Target Repository owns its Repository Profile,
  architecture, commands, glossary, and repository map. A Run records the
  profile by path, hash, and source fingerprint, and refuses to advance when
  the profile is stale or fails its integrity check. Target Repository
  documentation never becomes Agentflow documentation.
- **Target.** Deeper profile discovery: inferring entry points, architecture,
  and repository-specific domain language rather than the current shallow map.

## Concurrency

- **Implemented.** Each Run receives a unique Git branch and Workspace at
  start. Concurrent Runs never share a checkout, and a Workspace is never the
  Target Repository's primary checkout.
- **Implemented.** Atomic stage claims prevent concurrent `advance` processes
  on the same Run: every `advance` acquires a claim through the
  compare-and-append claim events below before executing its stage and
  releases it, holder-scoped, when the stage completes or fails. A second
  `advance` while an unexpired claim is active fails with an error naming the
  holder and lease expiry and changes nothing. The guarantee is bounded by the
  lease: a stage that legitimately outlives its lease is recovered through
  `claim_expired` evidence rather than protected by mutual exclusion, as
  detailed in `run-kernel.md`.
- **Confirmed decision.** Single-writer enforcement uses compare-and-append
  claim events appended to the Run's own event log: a process claims a stage
  by appending a claim event, and the append succeeds only if the log is
  unchanged since it was read. Claims carry lease expiry so stale claims can
  be recovered. No separate lock file, lock service, or second store of claim
  state is introduced; the event log remains the only claim authority. This
  resolves what were previously open locking choices in the decision map.
- **Target.** Broader single-builder Workspace locking beyond the per-Run
  stage claim — for example, blocking any second writer on a Workspace that
  was not started through `advance` — remains unenforced.

## Work Graph

- **Implemented.** Work Items are owned by the Target Repository and stored as
  git-tracked JSONL under `.agentflow/work/`. Each Work Item is a versioned,
  validated object (`id`, `summary`, `acceptance_criteria`, `depends_on`); the
  Work Graph rejects duplicate ids, unresolvable dependencies, self-dependency,
  and cycles. Work-intent truth is born in the Work Graph, execution truth in
  Run Evidence; each keeps only references to the other, never copies — a Run
  captures a Work Item by `work_item_id` and content hash.
- **Implemented.** Work Graph persistence sits behind a replaceable backend
  interface (`WorkGraphBackend`: `read_items` / full-replace `write_items`),
  with the git-tracked JSONL store as the default implementation and an
  in-memory backend for tests. Backends own storage only: `load_work_graph`
  and `save_work_graph` always validate, so swapping backends cannot change
  Work Graph semantics. The JSONL backend's `write_items` deletes every
  `*.jsonl` under `.agentflow/work/` and writes the replacement set to
  `graph.jsonl`, so a save fully replaces a hand-split layout instead of
  merging with it. The write path exists for upcoming Work Graph mutators
  (structured Discoveries); today's production callers are read-only.
- **Implemented.** Ready work is computed from the Work Graph's dependency
  relationships and the completion set whenever it is needed, never stored.
  `agentflow work list` validates and prints the graph; `agentflow work ready`
  prints the items that are not yet complete and whose dependencies all are.
  Completion is read from Run Evidence: a Work Item is complete when a
  `human_approved` Run captured it.
- **Implemented.** Framing produces the Work Graph warm and interactively (the
  Agentflow-owned framing skill composes grilling, documentation, and
  decomposition) and ends in human approval before any Run consumes it; a cold
  Run never invents its own decomposition or parallelism. See
  [ADR 0005](../adr/0005-framing-is-warm-and-in-session.md).
- **Implemented.** A Run captures a Work Item into its immutable Task Spec by
  reference and content hash: `start --work-item <id>` reads the item from the
  Target Repository's Work Graph and records its summary and acceptance criteria
  plus a `source` of provider `work-graph`, the `work_item_id`, the capture
  timestamp, and the item's content hash. Later edits to the Work Item do not
  alter the captured Run; completion of that Run is what marks the item done for
  ready-work computation.
- **Target.** Agent Roles return structured Discoveries as part of their
  validated output contracts. Discoveries are applied to the Work Graph only
  by deterministic validation code; no agent mutates the Work Graph directly.
- **Target.** Automatic dispatch of a ready Work Item into its own gated Run;
  today ready work is computed and the operator captures each Run with
  `start --work-item`.
- **Implemented.** The cold planner stage has been retired in favor of warm
  framing (ADR 0005): a Run advances `ready` -> `built` directly by invoking the
  builder against the Task Spec, with no `plan_ready`/`plan.json` stage in
  between.

## Reconciliation

- **Implemented.** A single-pass `reconcile` command reads the Work Graph and
  Run Evidence, captures each ready Work Item that has no live Run into a new
  Run, and advances every graph-backed Run toward its next human gate. It acts
  only by issuing the same `start`/`advance` application-service calls the CLI
  uses, so all execution truth stays in the per-Run event logs; it writes no
  state directly and returns a decision report (dispatched, advanced, blocked,
  completed).
- **Implemented.** Reconciliation never advances a Run through a human gate: it
  only calls `advance`, never `approve`, so a Run stops at `awaiting_human`.
- **Target.** Capacity limits, stale-claim recovery, and re-dispatch of a
  failed Work Item's Run within reconcile.

## Agent Adapters

- **Confirmed decision.** A model provider without a working Agent Adapter
  must have one built, tested, and landed before Agentflow coordinates work
  through that provider. Coordinating work through an unadapted provider is
  not permitted as a workaround.
- **Implemented.** Claude, Cursor, and Codex adapters, plus a deterministic fake
  for tests. Their executables are overridable via `AGENTFLOW_CLAUDE`,
  `AGENTFLOW_CURSOR`, and `AGENTFLOW_CODEX`. Changing an adapter must not
  change workflow state semantics, verification rules, or approval authority.
- **Implemented.** The reviewer role runs read-only; the builder and
  tester roles are constrained by role instructions and the kernel's diff
  enforcement (the builder's reported `files_changed` must equal its
  authoritative diff, and the tester stays at or under the declared
  `test_paths`). The Cursor builder and tester additionally request the Cursor CLI's
  sandbox while auto-approving operations; the Claude adapter does not provide
  an operating-system sandbox for its writing roles.
- **Implemented.** Cursor output is treated as untrusted model text because the
  Cursor CLI has no documented JSON Schema output flag. The adapter prompts for
  one JSON object, extracts a candidate object from result text that may retain
  progress prose, validates it locally against the same role contracts, and
  makes at most two output attempts before failing the stage.
- **Implemented.** Recorded per-role model routing with suggestion fallback
  and per-invocation provenance in the Claude and Cursor adapters:
  `agentflow models` shows and records the user's adapter-specific role-to-model
  choices in `models.json` in Agentflow Home. Each invocation resolves
  `advance --model`, then the adapter's `AGENTFLOW_<ADAPTER>_<ROLE>_MODEL`
  environment variable, then recorded routing, then suggested defaults. The
  resolved model is recorded on the stage's `build_ready`,
  `repair_ready`, `tests_ready`, `tests_failed`, `review_ready`, or
  `review_blocked` event. The suggested defaults cover the tester role for both
  adapters; the fake and Codex adapters route no models and record no `model`
  field.
- **Implemented.** Live role observability for the Claude and Cursor adapters:
  each builder, tester, and reviewer stage streams the provider's output
  (`stream-json`) to a tailable `runs/<run-id>/<role>-transcript.jsonl`
  evidence file referenced from the stage event, and the read-only `watch`
  command follows a Run's events and growing transcript until it reaches a
  state requiring external action, rendering human-readable event and
  assistant/tool lines rather than raw stream-json (evidence on disk stays
  raw). Cursor transcripts include a local attempt marker before each bounded
  output attempt. The fake and Codex adapters produce no transcript. Streaming
  does not change workflow state, verification rules, or approval authority.
- **Implemented.** A read-only observability projection over Run Evidence and
  the Work Graph (`agentflow project`): it renders runs, work, and evidence,
  is rebuildable from events at any time, never writes, and is never consulted
  as workflow authority — `advance` / `approve` / `start` derive state only
  from event replay. Corrupt Run evidence is isolated like `list_runs`: invalid
  JSON lines and undecodable bytes do not abort the whole projection, and valid
  event lines before damage are preserved.
- **Target.** Capability-based model routing across providers, beyond the
  Claude adapter's recorded per-role routing.
- **Implemented.** A local read-only web UI over the observability projection
  for runs, work, evidence, and live role transcripts (`agentflow serve`). It
  renders the projection at `/api/projection` and streams each Run's events and
  growing `*-transcript.jsonl` files over Server-Sent Events, tailing the same
  evidence files the `watch` command follows. It never writes workflow state, is
  never consulted as workflow authority, and exposes no approve/start/mutate
  route — every non-GET method is refused. Run ids and filesystem paths are
  confined: `.`/`..` and separator-bearing run ids are rejected, a symlinked run
  directory whose real path escapes Agentflow Home `runs/` is omitted, and an
  evidence or transcript symlink that escapes its run directory is refused. A
  circular/self-referential evidence or transcript symlink is a confinement
  failure that skips that path without raising, so `/api/projection` and the SSE
  stream keep serving every sibling in-bounds Run.
- **Target.** Automatic adapter self-provisioning: Agentflow detecting a
  missing adapter and building, testing, and landing one through its own
  workflow before use. Until this exists, adapters are built through
  documented Bootstrap Development. The provisioning approach is an open
  choice in the [decision map](../decisions/agentflow-factory.md).
