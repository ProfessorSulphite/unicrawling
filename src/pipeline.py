#!/usr/bin/env python3
"""
DEPRECATED compatibility shim -- pipeline.py became src/orchestrator.py in C22.

The rewrite was not a move. Five things that had accumulated in pipeline.py were
not orchestration and now live where they belong:

  derive_uni_info           -> src/utilities/naming.py
  backup_existing_outputs   -> src/utilities/workspace.py
  setup_clean_logging       -> src/logger/setup.py
  generate_result_analytics -> src/inspector/analytics.py
  _source_ids_by_tier       -> StateManager.source_ids_by_tier()

They are re-exported here anyway, so a caller that imported them from pipeline
keeps working until C24.

Kept so existing call sites keep working while the refactor is in flight.
Removed in C24, once every import points at the new modules.

Patching a name *on this module* is a no-op for the real callers; point
monkeypatch at the defining module instead. tests/test_shim_hygiene.py enforces this.
"""
import sys
from pathlib import Path

# Kept ahead of the imports: `python src/pipeline.py` still has to work, and run
# that way sys.path[0] is src/, so the `src` package is not yet importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.inspector.analytics import (  # noqa: E402,F401
    audit_analytics,
    generate_result_analytics,
)
from src.logger.setup import setup_clean_logging  # noqa: E402,F401
from src.orchestrator import (  # noqa: E402,F401
    build_queue,
    compile_master_json,
    load_run_settings,
    main,
    run_batch_pipeline,
    run_master_pipeline,
)
from src.utilities.naming import derive_uni_info  # noqa: E402,F401
from src.utilities.workspace import backup_existing_outputs  # noqa: E402,F401

if __name__ == "__main__":
    sys.exit(main())
