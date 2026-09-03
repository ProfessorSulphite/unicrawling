"""
DEPRECATED compatibility shim -- universal_normalizer.py was split into
src/extractor/normalizers/ in C16.

  currency_tuition.py  currency labelling (never conversion, never invention)
  eligibility.py       eligibility block cleanup (invents nothing since C19)
  degree_names.py      canonical degree levels -- scaffold, filled in C17
  runner.py            global facts registry, per-program and payload walks

Kept so existing call sites keep working while the refactor is in flight.
Removed in C24, once every import points at the new modules.

Deliberately NOT re-exported: _GLOBAL_REGISTRY. `from X import name` binds by
value, so a shim copy of a rebindable global freezes at its initial None and
silently diverges from the real one.

Patching a name *on this module* is a no-op for the real callers; point
monkeypatch at the defining module instead. tests/test_shim_hygiene.py enforces this.
"""
from src.extractor.normalizers.currency_tuition import (  # noqa: F401
    COUNTRY_CURRENCY_MAP,
    apply_currency_and_tuition,
    resolve_universal_currency,
)
from src.extractor.normalizers.eligibility import (  # noqa: F401
    normalize_eligibility,
)
from src.extractor.normalizers.runner import (  # noqa: F401
    GLOBAL_FACTS_FILE,
    load_global_registry,
    normalize_universal_payload,
    normalize_universal_program,
)
