"""
DEPRECATED compatibility shim -- the implementation moved to
src/logger/notebook_logger.py in C8.

Kept so existing call sites keep working while the refactor is in flight.
Removed in C24, once every import points at the new module.

Deliberately does NOT re-export the `_logger_instance` singleton. `from X import name`
binds by value, so a shim-level copy would freeze at None while the real module
populated its own -- and anything resetting the singleton through this path would
silently fail to reset it. Nothing outside the module touches it today; reach it via
get_notebook_logger() if that ever changes.
"""
from src.logger.notebook_logger import (  # noqa: F401
    NotebookLifecycleLogger,
    config,
    get_notebook_logger,
    log_json_repaired,
    log_notebook_created,
    log_notebook_deleted,
    log_query_executed,
    log_source_uploaded,
)
