"""
DEPRECATED compatibility shim -- ingest.py was split into src/ingestor/ in C12.

  http_client.py         shared HTTP/2 pool
  notebook_lifecycle.py  notebook provisioning
  source_management.py   URL hygiene, pre-flight, upload
  quota_management.py    source cap (query budget stays in StateManager)
  health_sampling.py     pre-flight link health sampling (C13)
  readiness_polling.py   jittered backoff readiness checks

Kept so existing call sites keep working while the refactor is in flight.
Removed in C24, once every import points at the new modules.
"""
from src.ingestor.http_client import (  # noqa: F401
    BROWSER_HEADERS,
    close_http_client,
    get_http_client,
    http_session,
)
from src.ingestor.health_sampling import (  # noqa: F401
    HealthReport,
    run_health_check,
    select_health_sample,
)
from src.ingestor.notebook_lifecycle import _find_or_create_notebook  # noqa: F401
from src.ingestor.quota_management import resolve_source_cap  # noqa: F401
from src.ingestor.readiness_polling import (  # noqa: F401
    _extract_id,
    wait_for_sources_adaptive,
)
from src.ingestor.source_management import (  # noqa: F401
    IngestedSource,
    IngestResult,
    check_url_accessible,
    fetch_and_extract_text,
    ingest_university_sources,
    sanitize_url,
)
