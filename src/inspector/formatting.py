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
    """Parses numeric PKR tuition fee from fee string."""
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
