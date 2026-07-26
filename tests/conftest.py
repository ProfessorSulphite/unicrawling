import sys
from pathlib import Path

# Tests import the package as `src.*`; make the project root importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
