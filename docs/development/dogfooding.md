# Dogfooding Agentflow

Agentflow is dogfooding only during a Self-Hosted Run. Using Agentflow to create
a Workspace and then manually performing an unavailable stage is Bootstrap
Development and must be labeled as such.

## Current level: first supervised Self-Hosted Run completed

Agentflow can profile itself, invoke schema-constrained planner, builder, and
reviewer roles through a provider adapter, enforce planned paths, commit a
candidate, execute authoritative checks, replay state, and stop at a SHA-bound
human approval gate. These capabilities were built through documented
Bootstrap Development. The first supervised Self-Hosted Run has now completed:
Run 1f8ac06da1d748d2abc4cde29d698d83 carried one small Agentflow change
through planner, builder, checks, and reviewer, stopped at `awaiting_human`,
and its approved candidate 5c4c2961d57ee1a340402f3d0165b5085da82a8f was merged
manually after explicit approval.

## Minimum self-hosting threshold

A first supervised Self-Hosted Run requires all of the following:

- A target-local Repository Profile or repository map is available.
- A planner Agent Role produces a schema-validated plan.
- A builder Agent Role changes only the Run's Workspace.
- Agent Adapters are selectable without changing workflow semantics.
- An authoritative check command executes outside agent reasoning.
- Check results become Run Evidence.
- The workflow records `awaiting_human` only after checks pass.
- Human approval is recorded by command and bound to the exact candidate SHA.
- A fresh process can replay the Run and continue from the recorded state.

Merge and deployment automation are not required for the first supervised
Self-Hosted Run. The Merge Agent and Post-Merge Verification now cover merging
and its verification; deployment remains manual after approval.

## Procedure once the threshold is met

```bash
cd /path/to/agentflow
agentflow init
agentflow profile --check "python3 -m unittest discover -s tests -v"
git add .agentflow/repository-profile.json
git commit -m "Add Agentflow repository profile"
agentflow start "<one small Agentflow improvement>"
agentflow advance <run-id> --adapter claude
agentflow advance <run-id> --adapter claude
agentflow advance <run-id>
agentflow advance <run-id> --adapter claude
agentflow status <run-id> # must report awaiting_human and candidate_sha
```

The workflow—not the chat session—must then invoke the configured planner and
builder, run authoritative checks, and stop at `awaiting_human`. The human
reviews the exact diff and records approval with:

```bash
agentflow approve <run-id> --approved-by <identity>
```

Review `git diff <base-sha>..<candidate-sha>` in the Run's Workspace before
approval. If any required stage is performed manually, record that gap and
classify the Run as Bootstrap Development.

## Session continuity

At the end of every development session:

1. Update architecture docs when implemented behavior changes.
2. Update `CONTEXT.md` when domain language changes.
3. Add an ADR only for a hard-to-reverse, surprising trade-off.
4. Update `docs/roadmap.md` with completed and next slices.
5. Run the full test suite and skill validator.
6. Commit and push a clean revision.
7. Generate a temporary handoff that references these durable artifacts rather
   than duplicating them.
