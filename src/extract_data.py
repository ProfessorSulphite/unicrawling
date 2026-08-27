"""
DEPRECATED compatibility shim -- extract_data.py was split into
src/extractor/crawlers/ in C15.

  json_repairing.py     fence/citation stripping, balanced-span JSON recovery,
                        pydantic validation against the target type
  notebook_querying.py  the five-query suite, QuerySpec/ExtractionReport, run_query
  exa_enriching.py      domain-scoped Exa fallback for the application portal URL
  runner.py             rankings registry, orchestration, notebook deletion

Kept so existing call sites keep working while the refactor is in flight.
Removed in C24, once every import points at the new modules.

Deliberately NOT re-exported: _RANKINGS_CACHE. `from X import name` binds by
value, so a shim copy of a rebindable global freezes at its initial None and
silently diverges from the real one -- the trap that kept _logger_instance out of
the notebook_logger shim in C8 and the crawler singletons out of the
extract_links shim in C14.

Patching a name *on this module* is a no-op for the real callers; point
monkeypatch at the defining module instead. tests/test_shim_hygiene.py enforces this.
"""

from src.extractor.crawlers.exa_enriching import (  # noqa: F401
    exa_find_application_portal,
)
from src.extractor.crawlers.json_repairing import (  # noqa: F401
    ExtractionError,
    _CITATION_IN_STRING,
    _CITATION_LEADING,
    _CITATION_TRAILING,
    _CLOSERS,
    _FENCED_BLOCK_REGEX,
    _FENCE_REGEX,
    _TRAILING_COMMA_REGEX,
    _adapter_for,
    _balanced_span,
    _parses,
    _validate_against,
    extract_json_str,
    repair_and_validate_json,
    strip_citation_markers,
)
from src.extractor.crawlers.notebook_querying import (  # noqa: F401
    ExtractionReport,
    Q1Payload,
    Q1_PROMPT,
    QUERY_SUITE,
    QuerySpec,
    _JSON_CONTRACT,
    _PROGRAM_STRUCTURE,
    _ask,
    run_query,
)
from src.extractor.crawlers.runner import (  # noqa: F401
    apply_registry_facts,
    delete_notebook_after_success,
    extract_university_payload,
    load_rankings_registry,
    lookup_registry,
)
