# Separate workflow semantics from Agent Adapters

Agentflow owns state transitions, evidence, Git scope enforcement, check
execution, and approval authority. Agent Adapters expose one narrow invocation
interface and return schema-constrained role output. Provider-specific behavior
must not change workflow semantics.

This separation makes the workflow model-agnostic and permits deterministic
fake adapters for acceptance tests. It also means provider features are used
only behind the adapter boundary; an agent's success claim cannot replace Git,
command, or event evidence.
