# Agentflow factory decision map

Unresolved implementation choices for the Agentflow factory. Resolved
decisions do not live here: confirmed behavior belongs in the
[product contract](../architecture/product-contract.md) and hard-to-reverse
trade-offs in [ADRs](../adr/). When a ticket below is answered, record the
outcome in the appropriate durable document and remove the ticket.

## 1. Adapter self-provisioning approach

- **Blocked by:** none.
- **Type:** implementation choice.
- **Question:** When Agentflow encounters a provider without a working Agent
  Adapter, how should it build, test, and land one through its own workflow —
  a dedicated Run template with adapter-specific contract fixtures, a
  provider-capability probe followed by a generated adapter skeleton, or
  something else — while satisfying the adapter-first gate recorded in the
  product contract?
- **Answer:** open.

## 2. Workspace cleanup policy for abandoned Runs

- **Blocked by:** none. Stage claims and lease expiry are decided: the
  product contract records compare-and-append claim events in the Run's own
  event log, so recovery can already distinguish an abandoned Run from one
  actively claimed by another process. Only the cleanup policy remains open.
- **Type:** implementation choice.
- **Question:** How and when are the Workspaces of abandoned Runs reclaimed —
  age-based garbage collection of worktrees, reclamation during a
  reconciliation pass, on-demand cleanup by the next command that touches the
  Run, or a combination?
- **Answer:** open.

## 3. Authenticated approval identity

- **Blocked by:** none — but this ticket blocks any network or UI mutation
  surface.
- **Type:** implementation choice.
- **Question:** `--approved-by` is currently unauthenticated free text,
  acceptable for a single-operator CLI but unacceptable behind any network
  surface. What mechanism authenticates the identity attached to an approval
  — operating-system user identity, signed approvals, an identity provider,
  or something else? No remote mutation surface may ship before approval
  identity is authenticated.
- **Answer:** open.
