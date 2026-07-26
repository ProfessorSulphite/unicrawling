#!/usr/bin/env python3
"""
Root CLI Executable Entrypoint for Education Counselor System
Delegates to developer-grade inspect_cli module.
"""
import sys
from pathlib import Path

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from src.inspect_cli import main

if __name__ == "__main__":
    main()
