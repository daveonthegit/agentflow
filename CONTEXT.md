# Agentflow glossary

## Agentflow

The deterministic workflow engine that coordinates engineering work across target repositories. Agentflow owns generic orchestration rules and never stores target-repository domain documentation.

## Target Repository

A repository on which Agentflow performs work. Its domain glossary, architectural decisions, project manifest, and repository map remain in that repository.

## Repository Profile

A target-local, versioned description of a Target Repository's structure, authoritative commands, and agent-relevant navigation. It is stored in the Target Repository; Agentflow retains only references and integrity metadata.

## Task Spec

The immutable statement of work captured when a Run begins. It may originate from a GitHub Issue or a local task file, but later edits to the source do not alter an active Run.

## Run

One execution of an engineering workflow against a captured Task Spec and an exact Target Repository revision.

## Run State

The current lifecycle position of a Run, derived from its ordered Run Evidence rather than stored as a separately mutable value.

## Workspace

The isolated checkout assigned to one Run. A Workspace must not be shared by concurrent builders or treated as the Target Repository's primary checkout.

## Run Evidence

Immutable records produced by a workflow run: artifacts, command results, approvals, and state-transition events. Run Evidence is the basis for verification and later learning.

## Artifact Store

The replaceable storage interface for persistent Run Evidence and temporary Workspaces. Its local adapter uses the operating system's standard application-data directory, outside both the Agentflow source repository and every Target Repository.

## Agentflow Home

The platform-specific application-data root used by the local Artifact Store. It may be overridden for CI and isolated testing without changing workflow semantics.

## Agent Role

A named set of responsibilities, permissions, instructions, and output contracts used by a workflow stage. Planner, builder, tester, reviewer, and merger are common roles, but they are configuration rather than hard-coded engine types.

## Agent Adapter

A provider-specific implementation that invokes a model or deterministic fake for an Agent Role. Changing an Agent Adapter must not change workflow state semantics, verification rules, or approval authority.

## Improvement Proposal

A reviewable change suggested from Run Evidence. It may improve a Repository Profile, Agentflow skill, or workflow configuration, but it does not take effect until evaluated and human-approved.

## Adoption Gate

The explicit verification and human-approval condition that an Improvement Proposal must pass before changing an Agentflow baseline.

## Approved Revision

The exact commit SHA accepted by a human approval. Any subsequent code change invalidates that approval and requires the resulting revision to pass verification and approval again.

## Candidate Revision

The exact commit SHA produced by the builder and subjected to authoritative checks and review. It becomes an Approved Revision only after explicit human approval while the Workspace remains clean at that SHA.

## Merge Agent

A constrained executor that may merge an Approved Revision only after deterministic gates confirm that approval is current, required checks pass for the exact merge candidate, and repository policy permits the operation. It records merge and post-merge evidence but does not grant approval itself.

## Post-Merge Verification

Authoritative checks run against the resulting target-branch commit after a Merge Agent completes a merge. Failure stops further shipping and produces Run Evidence for a human-reviewed recovery.

## Recovery Proposal

A reviewable response to a failed Post-Merge Verification, such as a revert or forward fix. Agentflow may prepare it, but it requires human approval before execution.

## Bootstrap Development

Work used to create a workflow capability that Agentflow cannot yet coordinate itself. Bootstrap Development must not be presented as a completed Agentflow stage.

## Self-Hosted Run

A Run whose Target Repository is Agentflow and whose required stages are coordinated and evidenced by Agentflow itself. Manual work around an unavailable stage remains Bootstrap Development, not a Self-Hosted Run.
