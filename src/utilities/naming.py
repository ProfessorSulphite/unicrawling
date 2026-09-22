"""
Deriving a university's identity from its URL.

Leaf layer: no imports out of utilities. The slug this produces is the key
everything else is filed under -- the state row, the per-university JSON, the
links file -- so it has to be derived one way, in one place.
"""
from typing import Optional, Tuple
from urllib.parse import urlparse


SATELLITE_CAMPUS_PREFIXES = {
    "qatar", "weill", "abudhabi", "shanghai", "galveston",
    "davis", "irvine", "riverside", "sandiego", "santacruz", "merced", "berkshire",
}

KNOWN_SATELLITE_NAMES = {
    ("qatar", "tamu"): "Texas A&M University at Qatar",
    ("weill", "cornell"): "Weill Cornell Medicine",
    ("abudhabi", "nyu"): "NYU Abu Dhabi",
    ("shanghai", "nyu"): "NYU Shanghai",
    ("galveston", "tamu"): "Texas A&M University at Galveston",
}


def derive_uni_info(url: str, name_override: Optional[str] = None) -> Tuple[str, str, str]:
    """Derives uni_name, uni_slug, and uni_domain from target URL, handling satellite subdomains."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]

    parts = domain.split(".")
    # Detect satellite / branch campus subdomains (e.g. qatar.tamu.edu, weill.cornell.edu)
    if len(parts) >= 3 and parts[0] in SATELLITE_CAMPUS_PREFIXES:
        sub = parts[0]
        parent = parts[1]
        slug = f"{parent}-{sub}"
        if name_override:
            name = name_override
        elif (sub, parent) in KNOWN_SATELLITE_NAMES:
            name = KNOWN_SATELLITE_NAMES[(sub, parent)]
        else:
            name = f"{parent.upper()} ({sub.capitalize()})"
    else:
        slug = parts[0] if parts else "university"
        if name_override:
            name = name_override
        else:
            name = slug.upper()

    return name, slug, domain

