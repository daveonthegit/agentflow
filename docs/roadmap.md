# Agentflow roadmap

Agentflow's goal is to make model-produced changes trustworthy (see the
[product contract](architecture/product-contract.md)). Work splits into a warm,
interactive **Framing** half that produces an approved Work Graph and a cold,
deterministic **Execution** half that builds and gates it. Each milestone is a
working vertical slice with executable acceptance tests; roles are added only
when the kernel can record and verify their outputs, and the concurrency and
evidence-integrity foundation is fixed before anything concurrent builds on it.

## Where things stand

The deterministic kernel is in place: immutable task and repository snapshots,
isolated Git worktrees, append-only event evidence with replayed state,
compare-and-append stage claims with lease recovery, authoritative checks run
outside the model, and human approval bound to an exact candidate SHA. On top of
it: the builder, tester, and reviewer roles with a Cursor/Claude/Codex/fake
adapter boundary; the adversarial tester gate; Workspace-integrity enforcement
(git hooks, `core.hooksPath`, ignored files); and the concurrency foundation
(atomic sequenced append, claim-guarded approve, expired-lease guard).

The warm/cold seam is connected end to end: the `framing` skill composes the
grill → document → decompose flow into an approved Work Graph; Work Items are
git-tracked JSONL with ready work computed from dependencies; and
`start --work-item` captures an item into a gated Run by id and content hash,
with completion derived from Run Evidence. The cold planner and its file-list
confinement have been retired in favor of framing (ADR 0005).

## Remaining work is tracked as a Work Graph, not here

Open work lives in Agentflow's own Work Graph under
[`.agentflow/work/`](../.agentflow/work/), so "what's left" is *computed*, never
a hand-maintained list that drifts from reality:

```bash
agentflow work list    # the whole graph, validated
agentflow work ready   # the items actionable now (dependencies satisfied)
agentflow work drift   # commits landed since approval that escaped the graph
```

A slice is closed by editing its Work Item out of the graph in the same commit
that ships it; git history is the completion record. The dependency edges in
the graph encode ordering that would otherwise be prose here — merge and
shipping work was gated behind the Merge Agent, and evidence-driven
improvement behind the observability projection. With the shipping chain
complete (merge, post-merge verification, and deployment adapters), the graph
is currently empty: new work enters through Framing, not by editing this
document. This document keeps only the narrative; the status is the graph.
