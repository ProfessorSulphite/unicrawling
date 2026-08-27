"""
Phase 3 schema extraction, split out of the 725-line src/extract_data.py in C15.

  json_repairing.py     fence/citation stripping, balanced-span JSON recovery,
                        pydantic validation against the target type
  notebook_querying.py  the five-query suite, QuerySpec/ExtractionReport, run_query
  exa_enriching.py      domain-scoped Exa fallback for the application portal URL
  runner.py             rankings registry, orchestration, notebook deletion

Dependency order is a strict DAG and must stay one:
    json_repairing <- notebook_querying <- runner
    exa_enriching  <- runner
Nothing here may import runner.
"""
