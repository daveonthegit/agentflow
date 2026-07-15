# Domain Docs

## Before exploring, read these

- `CONTEXT.md` at the repository root, or `CONTEXT-MAP.md` if it exists.
- `docs/adr/` for decisions that affect the area of work.

If these files do not exist, proceed silently. The `domain-modeling` skill creates them lazily when the project resolves a term or a hard-to-reverse decision.

## File structure

This is a single-context repository:

```text
/
├── CONTEXT.md
├── docs/adr/
└── src/
```

## Use the glossary's vocabulary

When naming a domain concept in an issue, proposal, hypothesis, or test, use the term defined in `CONTEXT.md`. If a concept is absent, record the gap for `domain-modeling` rather than inventing competing terminology.

## Flag ADR conflicts

Surface any conflict with an existing ADR explicitly instead of silently overriding it.
