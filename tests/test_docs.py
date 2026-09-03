"""
Documentation that cannot silently rot.

COMMANDS.md's stated gate is "every command shown actually runs". Prose cannot
hold that on its own -- the file drifted into documenting `src/pipeline.py`,
`src/extract_links.py`, `query_qdrant.py`, `config.json` and two export formats
that C11/C11b had already deleted, and nothing failed. So the gate is a test.

Every fenced shell command in the docs is extracted and checked: the file it
invokes must exist, and where the command is safe to run (`--help`, and any
subcommand's help) it is actually executed.
"""
import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOCS = ["README.md", "COMMANDS.md", "AGENTS.md", "CHANGELOG.md"]

# Things C11/C11b/C22/C23/C24 removed. A doc mentioning them as live is stale.
RETIRED_TOKENS = [
    "query_qdrant.py",
    "src/pipeline.py",
    "src/extract_links.py",
    "src/extract_data.py",
    "src/inspect_cli.py",
    "src/universal_normalizer.py",
    "--format qdrant",
    "--format pinecone",
    "sync_qdrant",
]

_FENCE = re.compile(r"```(?:bash|sh|shell|console)\n(.*?)```", re.S)


def _doc_paths():
    return [REPO / d for d in DOCS if (REPO / d).exists()]


def commands_in(text):
    """Every `python3 ...` / `pytest ...` invocation inside a shell fence."""
    out = []
    for block in _FENCE.findall(text):
        for raw in block.splitlines():
            line = raw.strip()
            if line.startswith("#") or not line:
                continue
            line = line.lstrip("$ ").strip()
            if "<" in line or ">" in line:
                continue  # a placeholder like `cli.py <subcommand>`, not a command
            if line.startswith(("python3 ", "python ", "pytest")):
                out.append(line)
    return out


# ----------------------------------------------------------- staleness --

@pytest.mark.parametrize("doc", DOCS)
def test_no_doc_references_a_deleted_module_or_format(doc):
    path = REPO / doc
    if not path.exists():
        pytest.skip(f"{doc} not present")
    text = path.read_text(encoding="utf-8")

    # A CHANGELOG legitimately names what was removed, in the past tense.
    if doc == "CHANGELOG.md":
        pytest.skip("a changelog records removals by name; that is its job")

    offenders = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        # A line that is explicitly about the removal is allowed to name it.
        if any(w in stripped.lower() for w in ("removed", "deleted", "replaced", "renamed",
                                               "no longer", "was ", "formerly", "retired")):
            continue
        for token in RETIRED_TOKENS:
            if token in line:
                offenders.append(f"{doc}:{line_no}: {token!r} in {stripped[:90]}")
    assert not offenders, "documentation references deleted things:\n" + "\n".join(offenders)


# ------------------------------------------------- the commands themselves --

def test_every_documented_command_names_a_file_that_exists():
    missing = []
    for path in _doc_paths():
        for cmd in commands_in(path.read_text(encoding="utf-8")):
            parts = cmd.split()
            if parts[0] == "pytest":
                continue
            if len(parts) > 1 and parts[1] == "-m":
                module = parts[2]
                if not module.startswith("src."):
                    continue  # a third-party module (playwright, pip), not ours
                target = REPO / Path(module.replace(".", "/"))
                if not (target.with_suffix(".py").exists() or (target / "__main__.py").exists()):
                    missing.append(f"{path.name}: python -m {module}")
            elif len(parts) > 1 and parts[1].endswith(".py"):
                if not (REPO / parts[1]).exists():
                    missing.append(f"{path.name}: {parts[1]}")
    assert not missing, "documented commands invoke files that do not exist:\n" + "\n".join(missing)


def test_every_documented_subcommand_is_a_real_subcommand():
    """Catches `cli.py export --format qdrant` and friends by asking argparse."""
    from src.inspector.cli import build_parser as inspector_parser
    from src.orchestrator import build_parser as orchestrator_parser

    valid_sub = set(inspector_parser()._subparsers._group_actions[0].choices)
    valid_run_flags = {
        opt for a in orchestrator_parser()._actions for opt in a.option_strings
    }

    bad = []
    for path in _doc_paths():
        for cmd in commands_in(path.read_text(encoding="utf-8")):
            parts = cmd.split()
            if len(parts) < 3:
                continue
            if parts[1].endswith("cli.py"):
                sub = parts[2]
                if not sub.startswith("-") and sub not in valid_sub:
                    bad.append(f"{path.name}: cli.py {sub}")
            elif parts[1].endswith("run.py"):
                for tok in parts[2:]:
                    if tok.startswith("--") and tok.split("=")[0] not in valid_run_flags:
                        bad.append(f"{path.name}: run.py {tok}")
    assert not bad, "documented commands use flags/subcommands that do not exist:\n" + "\n".join(bad)


def test_the_documented_help_commands_actually_run():
    """
    The subset that is safe to execute: anything ending in --help. These spend
    no quota and touch no network, so there is no reason not to run them.
    """
    failures = []
    checked = 0
    for path in _doc_paths():
        for cmd in commands_in(path.read_text(encoding="utf-8")):
            if not cmd.rstrip().endswith("--help"):
                continue
            argv = re.sub(r"^python3?\b", sys.executable, cmd, count=1)
            proc = subprocess.run(argv, shell=True, cwd=REPO, capture_output=True, timeout=120)
            checked += 1
            if proc.returncode != 0:
                failures.append(f"{cmd}\n    {proc.stderr.decode()[:300]}")
    assert checked, "no --help commands found in the docs to verify"
    assert not failures, "documented commands failed to run:\n" + "\n".join(failures)


def test_the_entry_points_table_matches_the_real_entry_points():
    """Both root entrypoints exist and are runnable (D4)."""
    for entry in ("run.py", "cli.py"):
        assert (REPO / entry).exists(), f"{entry} is documented as an entrypoint but is missing"
        proc = subprocess.run([sys.executable, entry, "--help"], cwd=REPO,
                              capture_output=True, timeout=120)
        assert proc.returncode == 0, f"{entry} --help failed: {proc.stderr.decode()[:300]}"


def test_the_documented_test_count_is_not_a_stale_number():
    """
    COMMANDS.md claimed "116 tests" long after the suite passed 500. A number
    that specific reads as a fact, so either keep it true or do not print it.
    """
    stale = []
    for path in _doc_paths():
        for m in re.finditer(r"\((\d+) tests\)", path.read_text(encoding="utf-8")):
            stale.append(f"{path.name}: hardcoded '{m.group(1)} tests'")
    assert not stale, (
        "a hardcoded test count will drift; describe the suite instead:\n" + "\n".join(stale)
    )
