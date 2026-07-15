---
name: agentflow
description: Run and manage software-engineering tasks through the Agentflow deterministic workflow CLI. Use when the user asks to use Agentflow, initialize Agentflow in a repository, start an Agentflow run, or continue work through its planning, verification, approval, merge, or shipping gates.
---

# Agentflow

Use the Agentflow CLI as the workflow authority. Treat model output as a
proposal and CLI state and recorded evidence as fact.

## Start in a repository

1. Work from the target repository root.
2. Run `command -v agentflow` to confirm the CLI is installed. If it is absent,
   explain that installing this skill alone does not install the executable.
   Direct the user to run
   `npx --yes github:daveonthegit/agentflow install` outside an active workflow
   run.
3. Run `agentflow init`. It is safe to rerun and must preserve existing project
   instructions.
4. Follow the repository's `AGENTS.md` and keep its project-specific knowledge
   in that repository. Do not copy it into the Agentflow source repository.

## Run the current MVP

Use a local JSON Task Spec containing at least a concise summary:

```json
{
  "summary": "Add a health endpoint"
}
```

Run it with an explicit evidence location:

```bash
agentflow run <task.json> --data-dir <agentflow-data-directory>
```

Report the returned run ID and state. Stop when the state is
`awaiting_human`; do not infer approval from conversation text.

## Preserve gate integrity

- Never claim a plan, build, check, review, approval, merge, or deployment
  happened unless Agentflow recorded corresponding evidence.
- Never bypass a failed or unavailable gate by editing run evidence manually.
- Never let an agent's prose override command exit status or test results.
- Require explicit human approval before merge.
- Treat the current CLI as a tracer-bullet MVP. Do not imply that unimplemented
  planner, builder, reviewer, merge, or deployment adapters have executed.
