# Agentflow glossary

## Agentflow

The deterministic workflow engine that coordinates engineering work across target repositories. Agentflow owns generic orchestration rules and never stores target-repository domain documentation.

## Target Repository

A repository on which Agentflow performs work. Its domain glossary, architectural decisions, project manifest, and repository map remain in that repository.

## Repository Profile

A target-local, versioned description of a Target Repository's structure, authoritative commands, and agent-relevant navigation. It is stored in the Target Repository; Agentflow retains only references and integrity metadata.

## Task Spec

The immutable statement of work captured when a Run begins. It may originate from a GitHub Issue or a local task file, but later edits to the source do not alter an active Run.

## Run Evidence

Immutable records produced by a workflow run: artifacts, command results, approvals, and state-transition events. Run Evidence is the basis for verification and later learning.

## Artifact Store

The replaceable storage interface for mutable Run Evidence and temporary workspaces. Its local adapter uses the operating system's standard application-data directory, outside both the Agentflow source repository and every Target Repository.

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

## Merge Agent

A constrained executor that may merge an Approved Revision only after deterministic gates confirm that approval is current, required checks pass for the exact merge candidate, and repository policy permits the operation. It records merge and post-merge evidence but does not grant approval itself.

## Post-Merge Verification

Authoritative checks run against the resulting target-branch commit after a Merge Agent completes a merge. Failure stops further shipping and produces Run Evidence for a human-reviewed recovery.

## Recovery Proposal

A reviewable response to a failed Post-Merge Verification, such as a revert or forward fix. Agentflow may prepare it, but it requires human approval before execution.
