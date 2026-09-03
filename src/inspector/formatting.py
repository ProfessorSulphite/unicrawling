"""
Display helpers shared by every inspector command.

Holds the one `console` the whole package prints through, so output ordering and
Rich's own state stay consistent no matter which module renders. Nothing here
reads the corpus or reaches the network -- it is the leaf of this package.
"""
import re
from typing import Any, Dict, Optional

from rich.console import Console

console = Console()


def show(value: Any, empty: str = "N/A") -> str:
    """
    Render a payload value for display.

    Since C19 the extractor writes null rather than inventing a value, so every
    string field reaching this layer can legitimately be None. Rich raises on a
    None cell and `None.upper()` raises before that, so a record the pipeline
    now produces routinely used to crash `inspect` and `diff` outright.

    Missing renders as missing. It is not the display layer's job to guess.
    """
    if value is None:
        return empty
    text = str(value).strip()
    return text or empty

# Regex constants
FEE_NUMBER_REGEX = re.compile(r"\d[\d,]*")


def format_deadlines(prog: Dict[str, Any], empty: str = "N/A") -> str:
    """Render application_deadlines for display. C18 made the field a list."""
    deadlines = prog.get("application_deadlines")
    if isinstance(deadlines, str):
        deadlines = [deadlines]
    if not isinstance(deadlines, list):
        deadlines = []
    cleaned = [str(d).strip() for d in deadlines if d is not None and str(d).strip()]
    return "; ".join(cleaned) if cleaned else empty


def extract_numeric_fee(fee_str: Optional[str]) -> Optional[float]:
    """
    Parse the first number out of a fee string.

    Currency-agnostic: it returns 1500 for both "EUR 1,500" and "PKR 1,500".
    Comparing the results across currencies is therefore meaningless -- see the
    note on the --max-fee filter in cli.py.
    """
    if not fee_str:
        return None
    matches = FEE_NUMBER_REGEX.findall(fee_str)
    if not matches:
        return None
    try:
        # Take the first matched number
        num = float(matches[0].replace(",", ""))
        return num
    except ValueError:
        return None
