"""
Phase 3 schema extraction, split out of the 725-line src/extract_data.py in C15.

  query_schemas.py      the six-block query suite, the consolidated prompts,
                        QuerySpec/ExtractionReport and the response models
  deepseek_extractor.py page fetching, corpus assembly and the DeepSeek engine
  gemini_extractor.py   the same two passes against the Gemini API
  json_repairing.py     fence/citation stripping, balanced-span JSON recovery,
                        pydantic validation against the target type
  exa_enriching.py      domain-scoped Exa fallback for the application portal URL
  verification.py       the Jev grounding gate over high-risk extracted claims

Dependency order is a strict DAG and must stay one:
    query_schemas <- deepseek_extractor <- gemini_extractor
    json_repairing, verification, free_search_enrichment <- both engines
Nothing here may import the orchestrator.
"""
