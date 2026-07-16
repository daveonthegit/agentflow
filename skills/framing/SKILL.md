---
name: framing
description: Decide WHAT Agentflow should build, warm and interactively, then emit an approved Work Graph the deterministic Runs consume. Use before starting Agentflow execution on a non-trivial task, when the work needs to be clarified, documented, and decomposed into dependency-ordered Work Items.
---

# Framing

Framing is the warm, interactive half of Agentflow: deciding *what* to build,
with a human in the loop, and producing an approved **Work Graph** that the
cold, deterministic Execution half turns into shipped code. Framing runs in the
operator's main session — it is NOT an `agentflow advance` stage — and Agentflow
owns only its output contract and the approval gate. See
[ADR 0005](../../docs/architecture/product-contract.md) and `CONTEXT.md` for the
`Framing` and `Work Graph` definitions.

This skill composes the existing Matt Pocock skills rather than reinventing
them. The seam between the warm and cold halves is the Work Graph: a cold Run
never invents its own decomposition or parallelism; it executes Work Items a
human already approved.

## Process

Work from the Target Repository root. Use its domain glossary vocabulary
throughout and respect its ADRs.

### 1. Grill to pin down intent and edge cases

Invoke `/grill-with-docs` (or `/grilling` with `/domain-modeling` when the
domain model itself is in flux). Resolve the actual goal, the edge cases, and
the decisions that are the user's to make — one question at a time, each with a
recommendation. Capture resolved terms in `CONTEXT.md` and hard, surprising
trade-offs as ADRs as they crystallize. Do not proceed until intent is shared.

### 2. Produce efficient documentation

Invoke `/to-prd` (or `/to-spec`) to synthesize what the grilling established
into a concise problem statement, solution, user stories, and the
implementation and testing decisions — including the test seams, preferring the
highest existing seam. Do not interview again; synthesize from context.

### 3. Decompose into a Work Graph

Invoke `/to-tickets` to break the work into tracer-bullet vertical slices, each
declaring the slices that block it. Then translate those tickets into Work Items
and write them as git-tracked JSONL under `.agentflow/work/` in the Target
Repository — one Work Item per line, conforming to the Work Item contract:

```json
{"id": "kebab-slug", "summary": "one line", "acceptance_criteria": ["…"], "depends_on": ["other-id"]}
```

- `id` is a stable, unique kebab-case slug.
- `depends_on` lists the ids of Work Items that must complete first; a ticket's
  blocking edges become these dependencies. Keep the graph acyclic.
- `acceptance_criteria` are the trimmed, unique conditions the reviewer will map
  evidence against.

Validate the graph deterministically with `agentflow work list` — it loads and
validates every `.agentflow/work/*.jsonl` (unique ids, resolvable dependencies,
no cycles) and prints the normalized graph. Fix any reported defect before
continuing. Preview ready work with `agentflow work ready`.

### 4. Get explicit human approval of the Work Graph

Present the Work Graph and its documentation. Framing ends only when the human
explicitly approves the graph. Conversational agreement is not approval — ask
directly. On approval, commit the `.agentflow/work/` files to the Target
Repository so the graph is git-tracked and content-addressable; each Work Item
is then captured into a Run's immutable Task Spec by `work_item_id` and content
hash. Do not edit a Work Item after a Run has captured it — material change
requires a new Run.

## Handing off to Execution

Once the Work Graph is approved and committed, the deterministic half takes
over. Compute ready Work Items with `agentflow work ready`, then capture each
independent item into its own gated Run with `agentflow start --work-item <id>`
(this records the item by id and content hash) and drive it through the
`agentflow` skill (build → checks → tester → review → human approval). Ready work
is recomputed from the graph and Run Evidence whenever needed; a Work Item is
done when its Run reaches human approval, which unblocks its dependents. Never
fabricate stage evidence, and never merge without explicit human approval — the
Execution gates are unchanged by Framing.
