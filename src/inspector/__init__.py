"""
Read-only view of what the pipeline produced: dashboard, data-quality auditing,
analytics, and the gated Supabase sync.

  formatting.py  the shared Rich console and the display helpers
  records.py     the read layer -- every command reaches the corpus through it
  dashboard.py   rendered views: one university, a diff, a search, the manifests
  analytics.py   dataset-wide counts, distributions and coverage
  auditor.py     per-programme required-field coverage and the push verdict
  sync.py        the export seam: CSV and the country-grouped JSON tree
  cli.py         argparse table and TUI menu -- the entry surface

Must not import orchestrator at module scope -- see Finding 6 in
resources/refactoring_todo.md. Use a function-local import where a command needs to
trigger a run. Only cli.py does, inside `retry_pipeline` and the `batch` command.
"""
from src.inspector.analytics import audit_analytics  # noqa: F401
from src.inspector.auditor import (  # noqa: F401
    audit_corpus,
    audit_records,
    readiness_verdict,
)
from src.inspector.cli import interactive_menu, main, retry_pipeline  # noqa: F401
from src.inspector.dashboard import (  # noqa: F401
    compare_universities,
    inspect_schema,
    inspect_state,
    inspect_university,
    search_programs,
)
from src.inspector.formatting import (  # noqa: F401
    FEE_NUMBER_REGEX,
    console,
    extract_numeric_fee,
    format_deadlines,
)
from src.inspector.records import (  # noqa: F401
    find_university_record,
    iter_all_records,
    load_all_records,
)
from src.inspector.sync import export_dataset  # noqa: F401
