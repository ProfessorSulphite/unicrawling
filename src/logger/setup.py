"""
Logging configuration for a pipeline run.

Separate from notebook_logger.py (which audits NotebookLM calls) and
pipeline_logger.py (which writes the run manifest): this only decides how noisy
third-party libraries are allowed to be on the console.
"""
import logging
from typing import Sequence

# Libraries that log at INFO on every request. At 83 universities their output
# buries the phase banners the operator is actually watching.
NOISY_LOGGERS: Sequence[str] = (
    "httpx",
    "urllib3",
    "asyncio",
    "crawl4ai",
    "ExtractData",
    "HEC_Link_Extractor",
)


def setup_clean_logging(level: int = logging.WARNING) -> None:
    """Raise the floor on the noisy third-party loggers, leaving ours alone."""
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(level)
