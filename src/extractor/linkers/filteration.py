"""
URL hygiene and the zero-garbage filter: scheme/extension/domain exclusion,
tokenised path exclusion, canonicalisation, dedupe keys, and recency decay.

Depends only on constants, so every other module here can import it.
"""

import html
import os

from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from src.extractor.linkers.constants import (
    EXCLUDED_DOMAINS,
    EXCLUDED_EXTENSIONS,
    EXCLUDED_PATH_PHRASES,
    EXCLUDED_PATH_TOKENS,
    EXCLUDED_SCHEMES,
    GENERIC_LINK_TEXTS,
    INVISIBLE_CHARS_REGEX,
    MULTI_SLASH_REGEX,
    OPEN_ENDED_GRACE_YEARS,
    OPEN_ENDED_YEAR_REGEX,
    PATH_SEPARATOR_REGEX,
    PATH_TOKEN_SPLIT_REGEX,
    TRACKING_PARAMS,
    YEAR_TOKEN_REGEX,
    logger,
)


def _tokenize_path(path: str) -> List[str]:
    """Split a URL path into lowercase alphanumeric tokens."""
    return [t for t in PATH_TOKEN_SPLIT_REGEX.split(path.lower()) if t]


def is_excluded_path(url: str) -> bool:
    """
    Decide whether a URL is administrative noise, using EXACT PATH TOKEN matching.

    Token matching rather than substring matching is the whole point of this
    function. Under the previous substring rule these were all wrongly deleted:
        /programs/bs-business-administration   (matched "admin")
        /programs/bs-accounting-and-auditing   (matched "audit")
        /admissions/apply-online-portal        (matched "portal")
    while the noise it was meant to catch (/wp-admin/, /news/) is still caught
    here -- by the "wp-admin" phrase rule and the "news" token rule respectively.
    """
    parsed = urlparse(url)
    path = parsed.path.lower()
    host = parsed.netloc.lower()

    # Exclude video hosts and social media URLs (cannot be ingested as NotebookLM web documents)
    if any(h in host for h in ("youtu.be", "youtube.com", "vimeo.com", "facebook.com", "twitter.com", "instagram.com", "linkedin.com")):
        return True

    for phrase in EXCLUDED_PATH_PHRASES:
        if phrase in path or phrase in host:
            return True

    if "onlineverification" in host or "verification" in path:
        return True

    tokens = set(_tokenize_path(path))
    if tokens & EXCLUDED_PATH_TOKENS:
        return True

    # Subdomain check: lms.uni.edu.pk is noise, portal.uni.edu.pk is a deliverable.
    host_labels = set(host.split("."))
    if host_labels & (EXCLUDED_PATH_TOKENS - {"media", "archive", "audit"}):
        return True

    return False


def sanitize_url(url: str) -> str:
    """
    Sanitizes URL path by stripping trailing hyphens, stray punctuation, and malformed characters.

    Prevents malformed/truncated URLs like '/program/bs-biotechnology-for-fall-2024-entry-'
    from being generated or passed downstream.
    """
    if not url:
        return url
    parsed = urlparse(url.strip())
    path = parsed.path
    if len(path) > 1:
        path = path.rstrip("-.,/")
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, parsed.fragment))


def normalize_url(url: str, base_url: str = "") -> Optional[str]:
    """
    Produce a canonical, fetchable URL, or None if the URL can never be ingested.

    Fixes observed in real NUST/LUMS crawl output:
      - '&amp;' left HTML-escaped inside the query string
      - a U+200B zero-width space inside an MBBS programme slug
      - 'sines.nust.edu.pk//program/...' double slashes producing duplicate sources
      - utm_* / fbclid campaign params splitting one page into several sources
    """
    if not url:
        return None

    url = html.unescape(html.unescape(url.strip()))
    url = INVISIBLE_CHARS_REGEX.sub("", url)

    if url.lower().startswith(EXCLUDED_SCHEMES):
        return None

    parsed = urlparse(url)
    netloc = parsed.netloc.lower().rstrip(".")
    # Drop default ports so :443 and bare host do not become two sources.
    if netloc.endswith(":80") and parsed.scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and parsed.scheme == "https":
        netloc = netloc[:-4]

    path = MULTI_SLASH_REGEX.sub("/", parsed.path)
    if len(path) > 1:
        path = path.rstrip("-.,/")

    kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS]
    query = urlencode(sorted(kept))

    # Fragment is always dropped: #section is the same source document.
    res = urlunparse((parsed.scheme, netloc, path, parsed.params, query, ""))
    return sanitize_url(res)


def dedupe_key(url: str) -> str:
    """
    Identity key for exact-duplicate collapse. Ignores scheme and a leading 'www.'
    so http://uni.edu.pk/x and https://www.uni.edu.pk/x count as one source --
    they would otherwise consume two of the 60 per-notebook slots for one page.
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{parsed.path}?{parsed.query}"


def compute_year_decay_factor(text: str, now_year: Optional[int] = None) -> Tuple[float, str]:
    """
    Score recency against the *current* year, resolved at runtime.

    Returns (multiplier, human_readable_tag). The most recent year mentioned wins,
    so 'fall-2025-onward' and '2025-2026' are treated as current in 2026 rather
    than being penalised for also containing an older number.
    """
    now_year = now_year or datetime.now().year
    years = [int(y) for y in YEAR_TOKEN_REGEX.findall(text)]
    # Ignore implausible years (phone numbers, IDs) outside the academic window.
    years = [y for y in years if 2000 <= y <= now_year + 6]
    if not years:
        return 1.00, "Current / Timeless"

    latest = max(years)

    # "fall-2025-onward" / "2024-and-onwards" name an open-ended range whose start
    # year is in the past but which is still the policy in force. Treating the
    # start year as a point-in-time would wrongly decay the current curriculum.
    #
    # The rescue is bounded: a live NUST crawl surfaced "for-fall-2023-onwards" and
    # a 2022 variant, and granting those the full current-year boost would rank a
    # four-year-old scheme above this year's. Within the window they are boosted;
    # beyond it they are held neutral rather than decayed, since the page may still
    # be in force but is no longer evidence of currency.
    if OPEN_ENDED_YEAR_REGEX.search(text) and latest <= now_year:
        if latest >= now_year - OPEN_ENDED_GRACE_YEARS:
            return 1.15, f"{latest}-onward Up-to-Date (Boosted)"
        return 1.00, f"{latest}-onward Open-Ended (Neutral)"

    delta = latest - now_year

    if delta >= 0:
        return 1.15, f"{latest} Up-to-Date (Boosted)"

    factor = max(0.25, 1.00 + 0.20 * delta)
    return round(factor, 2), f"{latest} Historical (Decayed x{factor:.2f})"

def preprocess_and_filter_links(
    links: List[Dict[str, str]],
    base_url: str = "",
    exclude_keywords: str = "news|events"
) -> List[Dict[str, str]]:
    """
    Enforces the Zero Garbage Policy:
    - Filters out non-http schemes, static non-document assets, administrative noise, and external social media.
    - Dynamically filters out news, events, announcements, and custom pipe-separated exclude keywords (e.g. 'news|events').
    - Resolves vague anchor text using human-readable words from URL path slugs.
    - Normalizes and deduplicates URLs.
    """
    clean_links = []
    seen_keys = set()

    # Parse pipe-separated exclude keywords (e.g., 'news|events|convocation')
    exclude_list = [k.strip().lower() for k in exclude_keywords.split("|") if k.strip()]
    logger.info(f"Applying Exclusion Keyword Filter (token-matched): {exclude_list}")

    for link in links:
        raw_text = link.get("text", "").strip()

        normalized_url = normalize_url(link.get("href", ""), base_url=base_url)
        if not normalized_url:
            continue

        parsed = urlparse(normalized_url)
        domain = parsed.netloc.lower()

        if any(exc_domain in domain for exc_domain in EXCLUDED_DOMAINS):
            continue

        ext = os.path.splitext(parsed.path)[1].lower()
        if ext in EXCLUDED_EXTENSIONS:
            continue

        if is_excluded_path(normalized_url):
            continue

        # User-supplied exclude keywords, matched against path tokens and anchor
        # words rather than as raw substrings, so --exclude-keywords "news" cannot
        # take out "newsletter-subscription-for-prospective-students".
        if exclude_list:
            link_tokens = set(_tokenize_path(parsed.path)) | set(_tokenize_path(raw_text))
            if link_tokens & set(exclude_list):
                logger.debug(f"Filtered out link matching exclude pattern '{exclude_keywords}': {normalized_url}")
                continue

        if raw_text.lower() in {"skip to content", "skip to main content"}:
            continue

        key = dedupe_key(normalized_url)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        path_segments = PATH_SEPARATOR_REGEX.sub(' ', parsed.path)
        path_words = " ".join([w for w in path_segments.split() if len(w) > 1 and not w.isdigit()])

        effective_text = raw_text
        if not raw_text or raw_text.lower() in GENERIC_LINK_TEXTS:
            effective_text = path_words if path_words else normalized_url

        clean_links.append({
            "href": normalized_url,
            "text": effective_text,
            "raw_text": raw_text,
            "path_words": path_words,
        })

    logger.info(f"Preprocessing complete: Retained {len(clean_links)} clean counseling-relevant links (filtered out {len(links) - len(clean_links)} noise/news/events links).")
    return clean_links
