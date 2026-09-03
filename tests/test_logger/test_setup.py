"""
setup_clean_logging raises the floor on third-party loggers, not on ours.

At 83 universities, httpx and crawl4ai logging at INFO on every request bury the
phase banners the operator is watching. Getting the direction wrong -- silencing
our own loggers instead -- would hide exactly the warnings that matter, so the
second test is the one worth having.
"""
import logging

from src.logger.setup import NOISY_LOGGERS, setup_clean_logging


def test_the_noisy_libraries_are_raised_to_warning():
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)

    setup_clean_logging()

    for name in NOISY_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


def test_our_own_loggers_are_left_alone():
    ours = logging.getLogger("Ingest")
    ours.setLevel(logging.INFO)
    setup_clean_logging()
    assert ours.level == logging.INFO, "the pipeline's own logging was silenced"


def test_the_level_is_configurable():
    setup_clean_logging(level=logging.ERROR)
    assert logging.getLogger("httpx").level == logging.ERROR
    setup_clean_logging()


def test_every_named_logger_actually_exists_in_the_tree():
    """
    A name that no module logs under is dead configuration. "Linker" was in this
    list when it was written and no module used it.
    """
    import src.extractor.crawlers.runner  # noqa: F401
    import src.extractor.linkers.constants  # noqa: F401
    import src.ingestor.source_management  # noqa: F401

    known = set(logging.root.manager.loggerDict)
    unused = [n for n in NOISY_LOGGERS if n not in known]
    assert unused == [], f"these logger names are configured but never used: {unused}"
