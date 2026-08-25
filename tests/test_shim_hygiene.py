"""
Guard against the C12 failure class: a test that patches a name on a compatibility
shim silently stops testing anything.

`monkeypatch.setattr("src.ingest.check_url_accessible", fake)` rebinds the name in
the *shim's* globals. The real caller lives in src/ingestor/source_management.py
and resolves `check_url_accessible` from its own module globals, so it never sees
the fake -- the test keeps passing while exercising the unpatched production path.
That is exactly what happened to the pre-flight test during the ingest split, and
it was found by reading, not by the suite.

Patching an *attribute of an object* reached through a shim is fine and stays
allowed: `src.state.config.state_db_path` mutates the shared Config singleton,
which is the same object no matter which module you reach it through.

The shim list is discovered from the source tree rather than hardcoded, so this
guard covers the splits still to come (C14, C15, C20) and disappears on its own
as C24 deletes the shims.
"""
import ast
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
TESTS_DIR = REPO_ROOT / "tests"

_PATCH_FUNCS = {"setattr", "patch", "object"}


def discover_shim_modules():
    """Top-level `src` modules whose docstring declares them a compatibility shim."""
    shims = set()
    for path in sorted(SRC_DIR.glob("*.py")):
        try:
            doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
        except SyntaxError:  # pragma: no cover - a broken module fails elsewhere
            continue
        if doc and "compatibility shim" in doc.lower():
            shims.add(f"src.{path.stem}")
    return shims


def _is_importable_module(dotted):
    try:
        return importlib.util.find_spec(dotted) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def classify_patch_target(target, shims):
    """
    Return the offending shim module if `target` rebinds a module-level name on it.

    Splits at the longest dotted prefix that is an importable module. A remainder
    of exactly one component is a module-global rebind; anything longer reaches
    through the module to an object attribute and is safe.
    """
    parts = target.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:cut])
        if prefix in shims and _is_importable_module(prefix):
            return prefix if len(parts) - cut == 1 else None
    return None


def collect_string_patch_targets(tree):
    """Every string-literal first argument to a monkeypatch/mock patching call."""
    targets = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in _PATCH_FUNCS:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            targets.append((first.value, first.lineno))
    return targets


def test_no_test_patches_a_module_level_name_on_a_shim():
    shims = discover_shim_modules()
    if not shims:
        pytest.skip("no compatibility shims left in src/ -- guard no longer applies")

    violations = []
    for path in sorted(TESTS_DIR.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for target, lineno in collect_string_patch_targets(tree):
            offender = classify_patch_target(target, shims)
            if offender:
                rel = path.relative_to(REPO_ROOT)
                violations.append(
                    f"{rel}:{lineno} patches '{target}' on shim '{offender}'. "
                    f"Point it at the module that defines the name instead."
                )

    assert not violations, "Patching a shim's globals is a silent no-op:\n" + "\n".join(violations)


# ------------------------------------------------- the guard guards itself --

def test_classifier_flags_a_module_global_rebind_on_a_shim():
    shims = discover_shim_modules()
    assert "src.ingest" in shims, "src/ingest.py should still be a declared shim"
    assert classify_patch_target("src.ingest.check_url_accessible", shims) == "src.ingest"


def test_classifier_allows_attributes_reached_through_a_shim():
    shims = discover_shim_modules()
    # The Config singleton is one object; reaching it via a shim patches the real one.
    assert classify_patch_target("src.state.config.state_db_path", shims) is None


def test_classifier_ignores_non_shim_modules():
    shims = discover_shim_modules()
    assert classify_patch_target("src.ingestor.source_management.check_url_accessible", shims) is None
    assert classify_patch_target("httpx.AsyncClient", shims) is None
