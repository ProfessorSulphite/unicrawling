"""
DEPRECATED compatibility shim -- the implementation moved to src/utilities/json_io.py in C5.

Kept so existing call sites keep working while the refactor is in flight.
Removed in C24, once every import points at the new module.
"""
from src.utilities.json_io import *  # noqa: F401,F403
from src.utilities.json_io import (  # noqa: F401
    __all__,
    _default_record_key,
    _fsync_dir,
    logger,
)
