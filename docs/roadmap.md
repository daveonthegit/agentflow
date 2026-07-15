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

## Next: first supervised Self-Hosted Run

- Use the committed Agentflow Repository Profile and installed Codex adapter.
- Run one small Agentflow change through planner, builder, checks, and reviewer.
- Stop at `awaiting_human` for exact candidate review and explicit approval.
- Record gaps found during the Run as inputs to the next vertical slice.

## Next: adversarial verification

- Tester Agent Role that may modify tests but not production code.
- Harden the existing read-only reviewer with evaluation fixtures.
- Bounded builder-fix retry loops.
- Evaluation fixtures and regression evidence for role and prompt changes.

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
