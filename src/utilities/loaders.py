"""
Environment loading helpers.

Deliberately imports nothing from src.config: config.py calls load_dotenv() at
module scope, so a dependency in the other direction would be circular. The
project root is derived from this file's own location instead.
"""
import os
from pathlib import Path

# src/utilities/loaders.py -> src/utilities -> src -> project root
BASE_DIR = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path = BASE_DIR / ".env") -> None:
    """
    Minimal .env loader (no python-dotenv dependency).

    Existing environment variables win, so an explicitly exported key is never
    silently overridden by a stale file.
    """
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass
