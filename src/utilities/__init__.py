"""
Shared, dependency-free helpers: env loading, JSON/JSONL I/O, SQLite state, payload schema.

Nothing in this package may import from extractor/, ingestor/, inspector/ or orchestrator.
It is the bottom of the dependency graph.
"""
