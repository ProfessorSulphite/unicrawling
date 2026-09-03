#!/usr/bin/env python3
"""
Root entrypoint for the inspector (decision D4).

    python3 cli.py inspect itu
    python3 cli.py audit
    python3 cli.py interactive

`run.py` is the matching entrypoint for the pipeline. Two root files, one job
each, both preserved because every doc example starts at the repo root.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.inspector.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
