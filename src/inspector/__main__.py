"""
`python -m src.inspector` -- the package's own entry point.

Exists so the package can be run without `-m src.inspector.cli`, which trips a
RuntimeWarning: __init__.py has already imported cli by then, and runpy re-executes
a module it finds in sys.modules.
"""
from src.inspector.cli import main

if __name__ == "__main__":
    main()
