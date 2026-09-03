"""
Deterministic post-extraction normalization, split out of
src/universal_normalizer.py in C16.

  currency_tuition.py  currency labelling (never conversion) and tuition defaults
  eligibility.py       eligibility / admission-requirement defaults
  degree_names.py      canonical degree levels -- scaffold, filled in C17
  runner.py            global facts registry, per-program and payload walks

Dependency order:
    currency_tuition, eligibility <- runner
"""
