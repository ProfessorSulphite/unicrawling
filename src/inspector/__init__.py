"""
Read-only view of what the pipeline produced: dashboard, data-quality auditing,
analytics, and the gated Supabase sync.

Must not import orchestrator at module scope -- see Finding 6 in
resources/refactoring_todo.md. Use a function-local import where a command needs to
trigger a run.
"""
