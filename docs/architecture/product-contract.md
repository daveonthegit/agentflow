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
  state. `advance` performs one stage per invocation.
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
- **Target.** Explicit plan approval, a tester role, bounded builder-fix retry
  loops, a constrained Merge Agent, and Post-Merge Verification. Merge and
  deployment remain manual after approval until these exist.
- **Target.** Bounded repair transitions out of `changes_requested`, explicit
  plan and human rejection transitions, reconciliation, and Workspace cleanup
  after abandonment.

## Evidence

- **Implemented.** Run Evidence is append-only, sequence-numbered, and stored
  in Agentflow Home, outside both the Agentflow repository and every Target
  Repository. Run State is projected from event replay, never from an
  independently mutable status file.
- **Implemented.** Authoritative checks execute outside model reasoning
  against the exact candidate SHA, must leave the Workspace clean, and their
  raw results become Run Evidence. No agent report can override command exit
  status or recorded evidence.
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
- **Implemented.** Claude and Codex adapters, plus a deterministic fake for
  tests. Their executables are overridable via the `AGENTFLOW_CLAUDE` and
  `AGENTFLOW_CODEX` environment variables. Changing an adapter must not change
  workflow state semantics, verification rules, or approval authority.
- **Implemented.** Planner and reviewer roles run read-only; the builder role
  is constrained by role instructions and the kernel's planned-path diff
  enforcement rather than an operating-system sandbox in the Claude adapter.
- **Target.** Automatic adapter self-provisioning: Agentflow detecting a
  missing adapter and building, testing, and landing one through its own
  workflow before use. Until this exists, adapters are built through
  documented Bootstrap Development. The provisioning approach is an open
  choice in the [decision map](../decisions/agentflow-factory.md).
