"""
NotebookLM-side resource management: notebook lifecycle, source upload,
query-quota accounting, pre-flight link health sampling and source-readiness
polling.
"""
from src.ingestor.source_management import ingest_university_sources
from src.ingestor.notebook_lifecycle import purge_notebook_sources

__all__ = ["ingest_university_sources", "purge_notebook_sources"]
