# Investigation: work graph and web UI proposal (2026-07-15)

A review of a proposal to add a work graph, a scheduler, and a web UI on top
of the Agentflow kernel, evaluated against external prior art. Everything
adopted below is a target direction: none of it is implemented, and every
adopted behavior is recorded as Target (or as a confirmed mechanism decision)
in the [product contract](../architecture/product-contract.md), the
[decision map](../decisions/agentflow-factory.md), and the
[roadmap](../roadmap.md).

The review's verdict: the proposal's layering is right and most of it is
adoptable, but in a different order than proposed. The work graph and the
scheduler silently depend on kernel capabilities Agentflow has already
identified as missing (stage claims, recovery, repair transitions), so those
come first. The web UI is the last thing to build, not the first; the
observability gap it addresses is real but is cheaply closable at the CLI
layer today.

## Sources reviewed

- **The proposal document.** A layered architecture — append-only events →
  projector → read model → API → UI — plus a dependency-aware work graph, a
  continuous scheduler, and a web UI. The layering is already half true in
  Agentflow: the event log is the authoritative record, replay is the only
  state mechanism, and the application services are CLI-independent, so no
  phase requires a rewrite.
- **Beads (git-native issue tracker for agent workflows).** What was learned:
  work items can live as line-oriented records inside the repository they
  describe; claim and lease semantics let exactly one worker take an item;
  typed dependency relationships allow ready work to be computed rather than
  stored. The concepts are adopted; the tool itself is not a foundation (see
  the conflicts below).
- **Gastown (multi-agent fleet orchestrator).** What was learned: stalled
  work must be detected and recovered, and escalation should be recorded as
  events rather than handled ad hoc. Its tiered watchdog hierarchy solves a
  fleet-scale problem Agentflow does not have; the ideas are adopted inside a
  single reconciler instead.
- **Obsidian (link, backlink, and frontmatter knowledge model).** What was
  learned: plain-Markdown documentation with wiki links and typed frontmatter
  gains backlinks and graph navigation from existing editors at zero code
  cost, provided the documents stay in the Target Repository's own docs tree.

## Feature evaluation

Each evaluated feature with its outcome and the deciding reason.

| Feature | Outcome | Reason |
|---|---|---|
| Stage claims and leases | **Adopt** | Must be compare-and-append events in the Run's own event log; a separate claim store would create a second authority. |
| Run enumeration and explicit abandon | **Adopt** | Pure additive; retires manual run inventories in handoff files and makes abandoned Runs enumerable and terminal. |
| Bounded repair and rejection transitions | **Adopt** | `changes_requested` is a dead end today; repair must preserve all prior attempts as evidence. |
| Task snapshot extension (source reference, content hash, acceptance criteria) | **Adopt** | The task snapshot is already immutable; this is an additive schema change. |
| Work graph core (items, typed dependencies, computed ready work) | **Adopt, simplified** | Native JSONL backend in the Target Repository behind a backend interface; start with four relationship types, not six. |
| External work-tracker CLI adapter | **Defer** | Upstream Beads is now Dolt-backed and fast-moving; revisit as an optional integration once the backend interface is stable. |
| Structured discoveries with an inbox | **Adopt, simplified** | Discoveries ride the existing validated role-output contracts and are applied deterministically; the inbox is a CLI list before any UI; cap per Run and require a dedup key. |
| Scheduler / continuous reconciler | **Adopt, simplified, later** | A single-pass reconcile command with recorded decision events comes first; a daemon is only a loop around a proven command. |
| Stale-claim detection and abandoned-work recovery | **Adopt, simplified** | Lease expiry plus recovery on the next reconcile or advance; distinguishing stalled from slow needs heartbeat events. |
| Evidence enrichment (durations, timestamps, attempt number, environment fingerprint) | **Adopt** | Additive fields on existing check records. |
| Acceptance-criteria matrix | **Adopt, simplified** | Criteria live in the task snapshot and the reviewer maps each criterion to evidence; optional per Task Spec so documentation-only Runs are not forced through it. |
| Merge queue, PR integration, post-merge-verified state | **Defer** | Belongs to the existing merge-and-shipping milestone; adopt the `post_merge_verified` terminal state name in documentation now. |
| Knowledge conventions (docs directories, wiki links, typed frontmatter) | **Adopt** | Zero code; must stay in Target Repository docs with no parallel knowledge base inside Agentflow's own storage. |
| Knowledge tooling (backlinks, local graph, saved views) | **Defer** | Existing editors already read a docs folder and deliver most of this for free today; revisit once a read model exists. |
| Projector, read model, and read-only API | **Defer** | Prototype only after claims, enumeration, and repair land; the projection must be rebuildable from events and never written by the UI. |
| Web UI, read-only phase | **Defer** | Behind the read model; CLI observability first; the cost/benefit is poor while there is a single operator. |
| Web UI mutations (approve, create Runs) | **Defer** | Hard prerequisite: authenticated approval identity. Free-text approver names are not enough for any network surface. |
| Initiatives, waves, and critical-path views | **Defer** | The data model falls out of work-graph parent-child and blocking relationships; views are premature while a typical graph has fewer than ten nodes. |
| Fleet dashboard and tiered watchdogs | **Defer** | Adopt stall detection and recorded escalation inside one reconciler; reject the tiered hierarchy until many concurrent agents are routine. |
| Database-backed store, plugin system, UI-owned state, multi-repo federation, canvas authoring | **Reject** | Each conflicts with event-log authority, git-native ownership, or the minimal-dependency constraint. |

## Flagged conflicts

1. **Upstream Beads has moved to a Dolt-backed store.** Its default backend
   is now an embedded database, with line-oriented records demoted to an
   export format. That conflicts with the git-tracked JSONL direction adopted
   here, so the native minimal backend is the foundation and any external
   tracker adapter is an optional later integration — not the base layer.
2. **Scheduler versus human gates.** Automatic reconciliation must stop at
   every human gate: it may move a Run toward `awaiting_human`, never through
   it. This is a hard invariant to be enforced with tests, not a guideline.
3. **Approval identity.** `--approved-by` is unauthenticated free text —
   acceptable for a single-operator CLI, unacceptable behind any network
   surface. Authenticated approval identity is now a decision-map ticket and
   a prerequisite for any remote mutation surface.
4. **Dual-authority trap.** Claims must be kernel events that the work graph
   may mirror for ready-work computation; the kernel must never trust
   work-graph rows as claim authority.
5. **The proposal overstates the current state.** It lists bounded repair
   loops and recoverable execution as existing strengths to preserve; both
   are target behavior that must be built.
6. **Fleet-scale watchdog machinery is premature.** The underlying ideas
   (stall detection, escalation as recorded events, recovery) are adopted
   inside a single reconciler; the multi-tier hierarchy is not.

## Recommended slice order

1. Kernel claims — compare-and-append claim events; two racing `advance`
   invocations on one Run resolve to exactly one winner.
2. Run enumeration and explicit abandon.
3. Bounded repair and rejection transitions.
4. Task snapshot extension and evidence enrichment.
5. Work graph native backend with structured discoveries.
6. Single-pass reconcile with recorded decision events, capacity limits, and
   stale-claim recovery.
7. Acceptance-criteria matrix.
8. Projector and read-only observability projections.
9. Merge Agent, merge queue, and post-merge verification, per the existing
   roadmap milestone.

The proposal suggested read-only observability as the first slice. The review
rejects that: the smallest first slice with real value is kernel claims plus
run enumeration and abandon, because they are prerequisites for everything
concurrent, they resolve the hardest parts of the open decision tickets, and
they eliminate demonstrated recurring pain. A web UI with no claims, no
repair, and no run list underneath it would be a dashboard over dead ends.

Knowledge conventions (wiki links, typed frontmatter, dated investigation
records like this one) start immediately at zero code cost.
