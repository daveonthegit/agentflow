# Agentflow starter

This is the first tracer bullet of a reusable agentic engineering workflow engine. It intentionally contains no model SDK, Git integration, or remote services.

## Run the test

```bash
python3 -m unittest discover -s tests -v
```

## Run the workflow

```bash
PYTHONPATH=src python3 -m agentflow run examples/task.json --data-dir .agentflow
```

The command prints a run identifier and `awaiting_human`. Its append-only event history is written to:

```text
.agentflow/runs/<run-id>/events.jsonl
```

## Current contract

`run` validates that the task is JSON, creates a unique run, records fake workspace/plan/check events, and pauses for human approval. The next test will add `status`, which must rebuild the reported state by replaying the event history in a new process.
