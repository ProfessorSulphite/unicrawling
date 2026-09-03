"""
Deriving a university's identity from its URL.

Leaf layer: no imports out of utilities. The slug this produces is the key
everything else is filed under -- the state row, the per-university JSON, the
links file -- so it has to be derived one way, in one place.
"""
from typing import Optional, Tuple
from urllib.parse import urlparse


def derive_uni_info(url: str, name_override: Optional[str] = None) -> Tuple[str, str, str]:
    """Derives uni_name, uni_slug, and uni_domain from target URL."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]

    parts = domain.split(".")
    slug = parts[0] if parts else "university"

    if name_override:
        name = name_override
    else:
        name = slug.upper()

    return name, slug, domain
