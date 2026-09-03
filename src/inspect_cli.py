#!/usr/bin/env python3
"""
DEPRECATED compatibility shim -- inspect_cli.py was split into src/inspector/ in C20.

  formatting.py  the shared Rich console and the display helpers
  records.py     the read layer -- every command reaches the corpus through it
  dashboard.py   rendered views: one university, a diff, a search, the manifests
  analytics.py   dataset-wide counts, distributions and coverage
  auditor.py     per-programme required-field coverage and the push verdict
  sync.py        the export seam: CSV and the country-grouped JSON tree
  cli.py         argparse table and TUI menu -- the entry surface

Kept so existing call sites keep working while the refactor is in flight.
Removed in C24, once every import points at the new modules.

`console` IS re-exported, deliberately: it is a single Rich Console object that
every module in the package prints through, never rebound, so the shim copy is
the same object. The C8/C14/C16 hazard is a shim copy of a *rebindable* global
freezing at its initial value; this one has no such initial value to freeze at.

Patching a name *on this module* is a no-op for the real callers; point
monkeypatch at the defining module instead. tests/test_shim_hygiene.py enforces this.
"""
import sys
from pathlib import Path

# Kept ahead of the imports: `python src/inspect_cli.py <cmd>` still has to work,
# and run that way sys.path[0] is src/, so the `src` package is not yet importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.inspector.analytics import audit_analytics  # noqa: E402,F401
from src.inspector.cli import (  # noqa: E402,F401
    interactive_menu,
    main,
    retry_pipeline,
)
from src.inspector.dashboard import (  # noqa: E402,F401
    compare_universities,
    inspect_notebooks,
    inspect_schema,
    inspect_state,
    inspect_university,
    search_programs,
)
from src.inspector.formatting import (  # noqa: E402,F401
    FEE_NUMBER_REGEX,
    console,
    extract_numeric_fee,
    format_deadlines,
)
from src.inspector.records import (  # noqa: E402,F401
    find_university_record,
    iter_all_records,
    load_all_records,
)
from src.inspector.sync import export_dataset  # noqa: E402,F401

if __name__ == "__main__":
    sys.exit(main())
