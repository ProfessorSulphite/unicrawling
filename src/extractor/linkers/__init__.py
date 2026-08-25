"""
Phase 1 link extraction, split out of the 1412-line src/extract_links.py in C14.

  constants.py         exclusion rules, keyword tiers, compiled regexes, logging
  filteration.py       URL hygiene, tokenised exclusion, dedupe keys, year decay
  deduplication.py     canonical degree collapsing
  semantic_scoring.py  embedding model, tier classification, quota allocation
  crawling.py          Crawl4AI discovery and the shared browser pool
  runner.py            HEC discovery, the pipeline, exporters, CLI

Dependency order is a strict DAG and must stay one:
    constants <- filteration <- deduplication <- semantic_scoring <- runner
    constants <- crawling <- runner
Nothing here may import runner.
"""
