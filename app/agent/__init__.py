"""Phase 2 agentic interaction layer.

Design constraint (R21/R26): the agent NEVER computes a new decision and
NEVER writes to any system. It only calls read-only tools backed by the
Phase 1 deterministic engines/data, then narrates the results in natural
language. All actual ranking/costing/explosion logic lives in
`app.decision_core.*` and is unchanged by this package.
"""
