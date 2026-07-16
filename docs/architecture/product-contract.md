# Agentflow product and architecture contract

This is the canonical contract for the Agentflow factory: the agreed behavior
Agentflow must exhibit, with every statement classified as implemented, as
target, or as a confirmed decision. Domain terms are defined in [`../../CONTEXT.md`](../../CONTEXT.md);
kernel mechanics, event contracts, and evidence layout are detailed in
[`run-kernel.md`](run-kernel.md); unresolved implementation choices live in
[`../decisions/agentflow-factory.md`](../decisions/agentflow-factory.md). This
document does not duplicate those sources.

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
  through planner, builder, checks, and reviewer stages driven by replayed
  state. `advance` performs one stage per invocation. The Task Spec may include
  optional `source` (`provider`, `work_item_id`, `captured_at` with an explicit
  timezone, and importer-supplied `content_hash`) and `acceptance_criteria`
  (trimmed unique non-empty strings). New Runs always store
  `acceptance_criteria` (empty is valid); `source` is omitted for direct human
  starts unless supplied by imported task JSON. Unknown Task Spec fields are
  rejected. Legacy summary-only task snapshots remain replayable. Material
  upstream task change requires a new Run — there is no snapshot refresh.
  `status` exposes `source` and `acceptance_criteria` only when present and
  non-empty; `list` stays concise without those fields. The complete frozen
  task object is passed to planner, builder, and reviewer.
- **Implemented.** Planner, builder, and reviewer outputs must satisfy strict
  versioned role contracts; the builder's authoritative Git diff must be a
  subset of planned paths and must equal its reported file list.
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
- **Implemented.** Bounded repair out of `changes_requested`: while fewer than
  `MAX_REPAIR_ATTEMPTS` (2) `repair_ready` events exist, `advance` invokes the
  builder with the original plan, latest review, current candidate, and
  one-based repair attempt; commits a new candidate; appends `repair_ready`;
  and re-enters `built` so checks and review rerun. After two repairs, the next
  repair attempt appends terminal `repair_exhausted` without invoking a model.
  Each build, check, review, and repair writes a distinct attempt-scoped
  artifact so earlier evidence is never overwritten. Attempt numbering also
  advances on `candidate_rebased`, so checks and reviews after a rebase cannot
  overwrite pre-rebase artifacts.
- **Implemented.** Explicit rejection: claim-guarded `reject` with required
  `--rejected-by` and optional `--reason`. From `planned` it appends terminal
  `plan_rejected`; from `awaiting_human` it appends terminal `human_rejected`
  bound to the candidate SHA. Rejection conversation text is never evidence.
  Rejected Runs cannot advance, approve, abandon, rebase, or be rejected again.
- **Implemented.** Human-attributed plan amendment: claim-guarded `amend-plan`
  with at least one `--add-path`, required `--amended-by`, and optional
  `--reason` widens the builder's allowed paths as recorded `plan_amended`
  evidence, never by editing immutable `plan.json`. It is permitted only from
  `planned` or `changes_requested`, projects no state, validates added paths
  like planned paths before appending, and only ever adds paths. The effective
  plan (the sorted union of `plan.json` and every amendment's `added_paths`)
  feeds and is enforced across the builder, repair, and reviewer stages, so a
  Run blocked only because the planner omitted a file can proceed instead of
  being abandoned. Conversational agreement is never amendment evidence.
- **Target.** Explicit plan approval, a tester role, bounded builder-fix retry
  loops, a constrained Merge Agent, and Post-Merge Verification. Merge and
  deployment remain manual after approval until these exist.
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
- **Target.** Approval-scoped merge automation: a Merge Agent may act only on
  a current Approved Revision after deterministic policy gates.

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

- **Target.** Work Items are owned by the Target Repository and stored as
  git-tracked JSONL under `.agentflow/work/`. Work-intent truth is born in
  the Work Graph, execution truth in Run Evidence; each keeps only references
  to the other, never copies.
- **Target.** Ready work is computed from the Work Graph's dependency
  relationships whenever it is needed, never stored as an independently
  mutable value.
- **Target.** Agent Roles return structured Discoveries as part of their
  validated output contracts. Discoveries are applied to the Work Graph only
  by deterministic validation code; no agent mutates the Work Graph directly.

## Reconciliation

- **Target.** A single-pass reconcile command reads the Work Graph, Run
  Evidence, Workspaces, and Git state, and acts only by issuing the same
  application-service calls the CLI uses. Every decision it makes is recorded
  as an event; it never writes state directly.
- **Target.** Reconciliation never advances a Run through a human gate. It
  may move a Run toward `awaiting_human`, never past it.

## Agent Adapters

- **Confirmed decision.** A model provider without a working Agent Adapter
  must have one built, tested, and landed before Agentflow coordinates work
  through that provider. Coordinating work through an unadapted provider is
  not permitted as a workaround.
- **Implemented.** Claude, Cursor, and Codex adapters, plus a deterministic fake
  for tests. Their executables are overridable via `AGENTFLOW_CLAUDE`,
  `AGENTFLOW_CURSOR`, and `AGENTFLOW_CODEX`. Changing an adapter must not
  change workflow state semantics, verification rules, or approval authority.
- **Implemented.** Planner and reviewer roles run read-only; the builder role
  is constrained by role instructions and the kernel's planned-path diff
  enforcement. The Cursor builder additionally requests the Cursor CLI's
  sandbox while auto-approving operations; the Claude builder does not provide
  an operating-system sandbox.
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
  resolved model is recorded on the stage's `plan_ready`, `build_ready`,
  `repair_ready`, `review_ready`, or `review_blocked` event. The fake and Codex
  adapters route no models and record no `model` field.
- **Implemented.** Live role observability for the Claude and Cursor adapters:
  each planner, builder, and reviewer stage streams the provider's output
  (`stream-json`) to a tailable `runs/<run-id>/<role>-transcript.jsonl`
  evidence file referenced from the stage event, and the read-only `watch`
  command follows a Run's events and growing transcript until it reaches a
  state requiring external action. Cursor transcripts include a local attempt
  marker before each bounded output attempt. The fake and Codex adapters
  produce no transcript. Streaming does not change workflow state,
  verification rules, or approval authority.
- **Target.** Capability-based model routing across providers, beyond the
  Claude adapter's recorded per-role routing.
- **Target.** A durable projection or UI over role transcripts beyond the
  line-tailing `watch` command — for example, a structured activity view
  spanning multiple Runs.
- **Target.** Automatic adapter self-provisioning: Agentflow detecting a
  missing adapter and building, testing, and landing one through its own
  workflow before use. Until this exists, adapters are built through
  documented Bootstrap Development. The provisioning approach is an open
  choice in the [decision map](../decisions/agentflow-factory.md).
