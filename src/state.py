"""
DEPRECATED compatibility shim -- the implementation moved to
src/utilities/state_management.py in C6.

Kept so existing call sites keep working while the refactor is in flight.
Removed in C24, once every import points at the new module.
"""
from src.utilities.state_management import *  # noqa: F401,F403
from src.utilities.state_management import (  # noqa: F401
    STATUS_SEQUENCE,
    TERMINAL_FAILURE,
    VALID_STATUSES,
    InvalidStatusError,
    QuotaExceededError,
    StateManager,
    config,
    logger,
)
