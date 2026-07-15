# Agentflow roadmap

The roadmap is ordered by dependency. Each milestone must be a working vertical
slice with executable acceptance tests; roles are added only when the kernel can
record and verify their outputs.

## Completed: installation and deterministic kernel

- Public one-command installer and global skill distribution.
- Idempotent Target Repository initialization.
- External Agentflow Home with environment and CLI overrides.
- Task and repository revision capture.
- Unique Git branch and Workspace per Run.
- Append-only event evidence and state replay.
- Explicit human approval transition.
- Removal of fabricated plan and check evidence.

## Completed: first supervised self-hosting foundation

- Discover or create a target-local Repository Profile.
- Map important directories, entry points, authoritative commands, and existing
  documentation without copying project knowledge into Agentflow.
- Record the profile path, revision, and integrity metadata in Run Evidence.
- Define freshness rules so stale maps are detected deterministically.
- Define versioned schemas for plan and role reports.
- Add planner and builder Agent Roles.
- Add at least one provider-specific Agent Adapter and one deterministic fake.
- Enforce allowed paths and one builder per Workspace.
- Execute the Target Repository's authoritative check command outside agents.
- Record candidate SHA, checks, and `awaiting_human` evidence.
- Bind approval to the exact candidate SHA.
- Resume a Run from a fresh process.

## Completed: first supervised Self-Hosted Run

- Used the committed Agentflow Repository Profile and an installed adapter.
- Ran one small Agentflow change through planner, builder, checks, and
  reviewer as Run 1f8ac06da1d748d2abc4cde29d698d83.
- Stopped at `awaiting_human`; the exact candidate was reviewed, explicitly
  approved, and merged as 5c4c2961d57ee1a340402f3d0165b5085da82a8f.
- Gaps found during the Run feed the next vertical slice.

## Completed: kernel claims and run lifecycle operations

- Compare-and-append stage claims in the Run's own event log, with lease
  expiry, so exactly one process can claim and execute a stage.
- Run enumeration across states without requiring a known run id.
- Explicit abandon operation that appends a terminal event.
- Bounded repair transitions out of `changes_requested` and explicit plan and
  human rejection transitions, preserving all prior attempts as evidence.

## Completed: task snapshot extension and check-evidence enrichment

- Optional Task Spec `source` (`provider`, `work_item_id`, `captured_at`,
  importer-supplied `content_hash`) and `acceptance_criteria`, with validation
  that rejects unknown fields while keeping legacy summary-only snapshots
  replayable.
- `start --acceptance-criterion` and `run task.json` preserve criteria and
  optional source; new Runs always store `acceptance_criteria` (empty allowed).
- `status` exposes source and non-empty criteria only; `list` stays concise.
- No snapshot refresh: material upstream task change requires a new Run.
- Check records enriched with `started_at`, `duration_ms`, shared per-stage
  `attempt`, and an allowlisted environment fingerprint only.

## Later: adversarial verification

- Tester Agent Role that may modify tests but not production code.
- Harden the existing read-only reviewer with evaluation fixtures.
- Bounded builder-fix retry loops.
- Evaluation fixtures and regression evidence for role and prompt changes.

## Later: target-repository work graph and reconciliation

- Work Items as git-tracked JSONL owned by the Target Repository under
  `.agentflow/work/`, behind a replaceable backend interface.
- Ready-work computation from typed dependency relationships rather than
  stored status.
- Structured Discoveries in role output contracts, applied to the Work Graph
  only through deterministic validation.
- Single-pass reconciliation that records every decision as an event and
  stops at every human gate.

## Later: read-only observability projections

- A projection over Run Evidence and the Work Graph, rebuildable from events
  at any time and never an authority.
- Read-only views of runs, work, and evidence; mutation surfaces are deferred
  until approval identity is authenticated.

## Later: merge and shipping

- Constrained Merge Agent operating only on an Approved Revision.
- Clean-environment CI gate and protected-branch policy checks.
- Post-Merge Verification and human-reviewed Recovery Proposals.
- Deployment adapters after merge safety is demonstrated.

## Later: evidence-driven improvement

- Generate Improvement Proposals from repeated Run Evidence.
- Evaluate proposals against fixed fixtures and historical failures.
- Require an Adoption Gate before changing skills, Repository Profiles, role
  prompts, or workflow defaults.
- Compare upstream skill changes and selectively adopt useful revisions.
