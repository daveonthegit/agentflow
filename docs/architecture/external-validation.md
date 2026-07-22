# External-worker validation

External-worker validation is Agentflow's narrow, first integration point for an
outside live-worker orchestrator such as Firstmate. It lets an outside
supervisor that already owns a worker session, an isolated worktree, and a
candidate revision ask Agentflow for the one thing Agentflow is authoritative
for — deterministic validation against the target's Repository Profile — without
handing over worktree lifecycle, agent spawning, human approval, or delivery.

## Architecture boundary

The boundary is a decided division of responsibility, enforced structurally
rather than by instruction:

| Concern | Owner |
| --- | --- |
| Worker sessions, worktree isolation, supervision, recovery | Firstmate (the external orchestrator) |
| Human approval and delivery (merge / push / PR) | Firstmate |
| Framing, Work Graphs, task tracking, Repository Profiles | Agentflow |
| Profile + candidate-identity verification, authoritative checks, durable validation evidence | Agentflow |

What the external path will never do — and cannot do, because the module imports
no Agent Adapter, no merger, and no deployment code, and has its own event
vocabulary:

- It never creates or deletes a worktree. Checks run **in place** in the
  caller's worktree; Agentflow does not `git worktree add` or remove anything.
- It never launches an agent or invokes an `AgentAdapter`.
- It never reaches `awaiting_human`. The external state projection
  (`EXTERNAL_STATE_BY_EVENT`) has no such state, so it is unreachable.
- It never merges, pushes, opens a PR, or records approval or delivery.

The existing adapter-driven `start`/`advance` Run workflow is unchanged and
remains the secondary, self-contained path; external validation is additive.

## Data model and evidence layout

An **External Validation** is a distinct record with its own append-only event
log, stored beside — never inside — the Run store:

```text
<Agentflow Home>/
├── runs/                      # unchanged adapter-driven Runs
└── external/
    └── <external-id>/
        ├── registration.json  # immutable registration snapshot
        ├── checks-1.json      # write-once authoritative-check evidence
        └── events.jsonl       # append-only, one-based sequence numbers
```

The caller's repository and worktree are referenced by absolute path and the
candidate by full SHA. Agentflow stores references and integrity metadata
(profile SHA-256 and source fingerprint), never a copy of the caller's state, so
the two systems keep separate operational homes.

### States

```text
registered ──validate (checks pass)──▶ validated        (terminal)
           └─validate (checks fail)──▶ validation_failed (terminal)
```

`validated` and `validation_failed` are terminal for a given candidate. To
re-check a different candidate, the caller registers a new External Validation;
the old evidence is never mutated.

### Events

| Event | Resulting state | Notes |
| --- | --- | --- |
| `external_registered` | `registered` | summary, acceptance_criteria, repository, worktree, candidate_sha, optional `external_ref`/`source` |
| `external_candidate_identified` | *(unchanged)* | the verified candidate SHA and worktree |
| `external_profile_captured` | *(unchanged)* | profile path, `profile_sha256`, `source_fingerprint`, `fresh` |
| `external_checks_passed` | `validated` | carries the checks artifact and candidate SHA |
| `external_checks_failed` | `validation_failed` | carries the checks artifact and candidate SHA |
| `external_validation_refused` | *(unchanged)* | a recognized refusal recorded as evidence; state is left unchanged |
| `claim_acquired` / `claim_released` / `claim_expired` | *(unchanged)* | the shared per-record stage claim |

Events carry contiguous one-based `sequence` numbers and are appended under the
same exclusive advisory lock the run kernel uses, so concurrent writers stay
serialized and the log stays replayable. Validation is claim-guarded with the
same mechanism `advance` uses, so two callers cannot double-run one validation.

## What verification enforces

At **registration** Agentflow verifies, without mutating the caller's checkout:

- the worktree path exists and is a git worktree (else *invalid path*);
- when `--repository` is supplied, the worktree shares that repository's git
  store (else *does not belong to repository*);
- the worktree is clean (else *dirty worktree*);
- the supplied candidate SHA (full or abbreviated) resolves to a commit and
  equals the worktree HEAD (else *mismatched SHA*);
- a `.agentflow/repository-profile.json` is committed and fresh — its source
  fingerprint matches the worktree (else *missing* / *stale Repository
  Profile*).

At **validation** Agentflow re-verifies all of the above under the stage claim
(worktree still present and a git worktree, HEAD still equals the registered
candidate, still clean, profile still present, fresh, and byte-identical to what
was captured), then runs the profile's `checks` in the worktree through the same
`run_authoritative_checks` helper the built stage uses. Evidence quality is
therefore identical to a Run's checks: the pinned `LANG`/`PYTHONHASHSEED`/`TZ`
environment, the allowlisted environment fingerprint, and per-check
`started_at`/`duration_ms`/`returncode`/`stdout`/`stderr`. A candidate that
leaves the worktree dirty fails, exactly as in the built stage.

Any recognized problem at validation time is recorded as an
`external_validation_refused` event and leaves the state at `registered`, so the
caller can fix the worktree and re-validate the same registration. Unsupported
transitions (validating an already-terminal record) are refused the same way.

## CLI contract

```bash
agentflow external register "<summary>" \
    --worktree <path> --candidate-sha <sha> \
    [--repository <path>] [--acceptance-criterion <text> ...] \
    [--external-ref <opaque caller handle>] [--data-dir <path>]

agentflow external validate <external-id> [--validated-by <identity>] [--data-dir <path>]
agentflow external status   <external-id> [--data-dir <path>]
agentflow external list     [--data-dir <path>]
```

- `register` prints JSON with `external_id`, `state` (`registered`),
  `candidate_sha`, `repository`, `worktree`, `repository_profile_path`, and
  `external_ref` when given. It exits nonzero with a clean message (no
  traceback) on any rejection and writes no record.
- `validate` prints JSON with `external_id`, `state`
  (`validated`/`validation_failed`), `passed`, `candidate_sha`, and the checks
  `artifact` path. **Its exit code mirrors the outcome: `0` when validated, `1`
  when the checks failed**, so a caller can branch on the process result alone.
  A recognized refusal also exits `1` with a message on stderr.
- `status` replays the record and returns the outcome and candidate identity,
  including `checks_artifact` and `validated_by` once validated.
- `list` prints every External Validation, sorted by first event.

`--data-dir` / `AGENTFLOW_HOME` resolve the Agentflow Home exactly as for Runs.

## Firstmate bridge requirements

A later Firstmate → Agentflow bridge needs only this contract:

1. **Provide a clean worktree at an exact SHA.** Firstmate already owns
   worktree isolation, so after a worker commits its candidate, the bridge
   passes that worktree path and the commit SHA to `external register`. The
   worktree must be clean and checked out exactly at the candidate.
2. **Ensure the target commits a fresh Repository Profile.** Validation is only
   as strong as the profile; registration refuses a missing or stale one. This
   is the same profile Agentflow Runs use — no external-only profile format.
3. **Carry the Firstmate task handle in `--external-ref`.** It is stored as
   opaque evidence and echoed back by `status`/`list`, so the bridge can
   correlate an External Validation with its Firstmate task without Agentflow
   knowing anything about Firstmate's schema.
4. **Branch on the `validate` exit code (or the JSON `passed`).** Agentflow
   returns a validated-or-failed outcome and durable evidence; **the decision to
   approve, merge, or deliver stays entirely with Firstmate.** Agentflow neither
   expects nor exposes an approval or delivery call on this path.
5. **Register a new validation per candidate.** Terminal outcomes are immutable;
   a new candidate (e.g. after a repair) is a new `external register`.

The bridge therefore never has to reach into Agentflow Home or reconstruct Run
state — the four subcommands above are the whole surface, and their JSON is the
stable integration contract.

## Module map

- `src/agentflow/external_validation.py` — registration, validation, status,
  and listing for the external path; imports no adapter/merge/deploy code.
- `src/agentflow/workflow.py` — `run_authoritative_checks`, the single check
  runner shared by the built stage, the post-tests re-check, and this path.
- `src/agentflow/run_kernel.py` — `append_event`/`acquire_claim`/`release_claim`
  accept an explicit `events_path` so the external log reuses the same locked,
  sequence-checked append and stage-claim machinery as Runs.
