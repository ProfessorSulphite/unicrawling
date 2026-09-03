"""
Deterministic post-extraction normalization, split out of
src/universal_normalizer.py in C16.

  currency_tuition.py  currency labelling (never conversion, never invention)
  eligibility.py       eligibility block cleanup (invents nothing since C19)
  degree_names.py      canonical degree levels (C17)
  program_fields.py    pre-C18 values moved onto the C18 fields
  runner.py            global facts registry, per-program and payload walks

Dependency order:
    currency_tuition, eligibility, degree_names, program_fields <- runner
"""
