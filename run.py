#!/usr/bin/env python3
"""
Root entrypoint for the pipeline (decision D4).

    python3 run.py --url https://itu.edu.pk
    python3 run.py --config config.json --dry-run
    python3 run.py --resume c_7

A thin root shim rather than making `python -m src.orchestrator` the only way
in: every doc example and every operator's muscle memory starts at the repo
root. `cli.py` is the matching entrypoint for the inspector.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.orchestrator import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
