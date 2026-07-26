#!/usr/bin/env python3
"""
================================================================================
HEC Recognized University & Education Counselor Link Extractor (Phase 1)
================================================================================
Author: Antigravity AI Engineering Team
Description:
    Phase 1 of the Education Counseling RAG Pipeline.
    This script extracts ultra-high-quality, high-relevance web links from
    official HEC-recognized Pakistani universities (e.g., NUST, ITU, LUMS, FAST)
    and specific university web pages.

    Key Features:
    - HEC Directory Discovery: Scrapes / loads official HEC-recognized university links.
    - Zero Garbage Policy: Aggressively filters out administrative noise, portals,
      logins, tenders, job vacancies, legal footers, and static assets.
    - Custom Noise & Pattern Exclude Filter (--exclude-keywords "news|events"):
      Filters out news, event announcements, press releases, convocations, seminars,
      and custom pipe-separated keyword patterns.
    - Priority Tiering & Semantic Scoring: Uses BAAI/bge-small-en-v1.5 to score
      and rank links into 4 Education Counseling Tiers:
        * Tier 1: Programs (BS, MS, PhD, Undergraduate/Postgraduate majors)
        * Tier 2: Fees, Admissions, Eligibility Criteria, Portals, Merit Lists
        * Tier 3: Faculties, Academic Departments, Schools, Campuses
        * Tier 4: FAQs, Prospectus, Scholarships, Hostel, Admission Contacts
    - Up-To-Date 2026 Information Filter (--uptodate):
        * When enabled (default: True), boosts 2025-2027 current links and penalizes
          outdated historical years (2010-2023).
    - Canonical Degree Deduplication Post-Processing:
        * Identifies and eliminates redundant degree variations (e.g. multi-campus
          subdomain mirrors and historical year intake duplicates like fall-2024 vs
          fall-2025-onward). Retains only the single highest-quality, most recent canonical link.
    - RAM & Flow Optimization: Generator-based processing, explicit tensor batching,
      and garbage collection to maintain low memory footprint.
    - Dual Output Generation:
        1. extracted_links.txt (Clean URLs only, 1 per line)
        2. extracted_links_detailed.txt (Full report: Rank, Tier, Score, Category, Keyword, URL)

Usage Examples:
    # Single university URL with news|events filtering and 2026 up-to-date filter:
    python3 extract_links.py --url https://nust.edu.pk --exclude-keywords "news|events|convocation" --max-links 100

    # HEC Recognized Universities Directory mode for top 5 universities:
    python3 extract_links.py --hec --hec-limit 5 --max-links 50
================================================================================
"""

import argparse
import asyncio
import gc
import html
import json
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

# Third-party dependencies
import requests
from bs4 import BeautifulSoup
from crawl4ai import (
    AsyncWebCrawler,
    BestFirstCrawlingStrategy,
    BrowserConfig,
    CacheMode,
    CrawlerRunConfig,
    KeywordRelevanceScorer,
)
from sentence_transformers import SentenceTransformer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from src.config import config
except ImportError:  # direct execution from inside src/
    from config import config

# ------------------------------------------------------------------------------
# 1. LOGGING & DIRECTORY SETUP
# ------------------------------------------------------------------------------
# Project root, not the src/ package directory -- logs and data belong beside the
# repository, not inside the source tree.
BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "loggings"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "extract_links.log"

# Configure root logger with both File and Stream handlers
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("HEC_Link_Extractor")


# ------------------------------------------------------------------------------
# 2. NOISE & EXCLUSION RULES (ZERO GARBAGE POLICY)
# ------------------------------------------------------------------------------

# Static assets to exclude
EXCLUDED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp",
    ".css", ".js", ".mp4", ".avi", ".mov", ".mp3", ".wav", ".flv",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".zip", ".rar", ".tar", ".gz", ".7z", ".exe", ".dmg", ".apk",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv"
}

# External non-academic / social media domains to exclude
EXCLUDED_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "instagram.com",
    "linkedin.com", "youtube.com", "tiktok.com", "pinterest.com",
    "whatsapp.com", "google.com", "t.me", "telegram.org",
    "github.com", "vimeo.com", "flickr.com"
}

# Non-counseling administrative noise, jobs, tenders, news, events, and legal footers.
#
# These are matched as EXACT PATH TOKENS, never as substrings. Substring matching
# was the single most destructive defect in the original filter: "admin" deleted
# every business-administration (BBA/MBA) URL, "audit" deleted auditing programmes,
# and "portal" deleted the application_portal_url that the data model designates as
# the pipeline's primary deliverable. Tokenisation makes each of those safe --
# "administration" and "auditing" are distinct tokens from "admin" and "audit".
EXCLUDED_PATH_TOKENS = {
    # Authenticated systems (no public counseling content behind a login)
    "login", "logout", "signin", "signup", "register", "auth", "sso",
    "lms", "moodle", "canvas", "blackboard", "webmail", "intranet",
    "admin", "dashboard", "password", "cpanel",
    # Operational noise
    "news", "event", "events", "announcement", "announcements",
    "seminar", "seminars", "workshop", "workshops", "convocation",
    "orientation", "webinar", "conference", "gallery", "photos", "media",
    "tender", "tenders", "bid", "bids", "rfp", "procurement",
    "career", "careers", "job", "jobs", "vacancy", "vacancies",
    "qec", "audit", "advancement", "notifications", "archive",
    # Legal / footer
    "disclaimer", "sitemap", "feed", "rss",
}

# Multi-token noise matched as a substring of the normalised path. Reserved for
# phrases that are unambiguous, so no legitimate academic URL can collide.
# NOTE "portal" is deliberately absent as a bare token -- an application portal is
# a Tier 2 deliverable. Only role-scoped portals (student/faculty/staff) are noise.
EXCLUDED_PATH_PHRASES = {
    "wp-admin", "wp-login", "wp-content", "wp-json",
    "student-portal", "faculty-portal", "staff-portal", "employee-portal",
    "alumni-portal", "hr-portal", "parent-portal", "applicant-login",
    "password-reset", "forgot-password", "reset-password",
    "press-release", "press-clippings", "rti-disclosure",
    "quality-enhancement", "treasurers-office", "downloads-archive",
    "privacy-policy", "terms-of-service", "terms-and-conditions",
    "cookie-policy", "code-of-conduct",
}

# Non-http(s) schemes that can never be ingested as a NotebookLM source.
EXCLUDED_SCHEMES = ("javascript:", "mailto:", "tel:", "whatsapp:", "skype:", "sms:", "fax:")

# Query parameters that carry no routing meaning; stripped so that the same page
# reached via different campaigns collapses to one source and does not burn two
# of the 60 per-notebook slots.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "fbclid", "gclid", "msclkid", "dclid", "yclid", "igshid", "mc_cid", "mc_eid",
    "_ga", "_gl", "ref", "referrer", "source", "campaign",
}

# Zero-width and invisible characters observed in real crawled university URLs
# (a U+200B inside an MBBS programme slug produced an unfetchable source).
INVISIBLE_CHARS_REGEX = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060\ufeff\u00ad]"
)

# Generic unhelpful anchor text patterns
GENERIC_LINK_TEXTS = {
    "click here", "read more", "view details", "details", "link", "more",
    "view", "download", "here", "know more", "pdf", "info", "site", "home",
    "skip to content", "top", "back to top", "learn more", "click", "see details"
}


# ------------------------------------------------------------------------------
# 3. SEMANTIC COUNSELOR KEYWORDS & PRIORITY TIERS
# ------------------------------------------------------------------------------

# Priority Tier 1: Degree Programs & Curriculum (Highest RAG value)
TIER1_PROGRAM_KEYWORDS = [
    "undergraduate degree programs", "postgraduate master phd programs 2026",
    "bs computer science software engineering", "bachelor of science business administration",
    "ms data science artificial intelligence 2026", "phd computer science electrical engineering",
    "degree requirements majors curriculum", "department course list syllabus"
]

# Priority Tier 2: Fees, Admissions, Eligibility & Portals
TIER2_ADMISSION_KEYWORDS = [
    "tuition fee structure semester charges 2026", "fee refund policy rules",
    "admission eligibility criteria minimum marks 2026", "online application portal apply now 2026",
    "admission schedule deadline entry test 2026", "merit list closing aggregate formula 2026"
]

# Priority Tier 3: Faculties, Departments & Campuses
TIER3_FACULTY_KEYWORDS = [
    "faculties academic departments schools", "faculty of computing engineering sciences",
    "department of business management humanities", "main campus regional sub campuses"
]

# Priority Tier 4: FAQs, Prospectus, Financial Aid & Contacts
TIER4_SERVICES_KEYWORDS = [
    "frequently asked questions admission faqs", "undergraduate prospectus handbook pdf 2026",
    "hec need based scholarships financial aid peef 2026", "hostel accommodation campus facilities",
    "admissions office contact phone email address"
]

# Master Mapping of Priority Tiers
PRIORITY_TIERS = {
    "Tier 1: Programs & Degrees": (1, TIER1_PROGRAM_KEYWORDS, 1.25),
    "Tier 2: Fees & Admissions": (2, TIER2_ADMISSION_KEYWORDS, 1.20),
    "Tier 3: Faculties & Departments": (3, TIER3_FACULTY_KEYWORDS, 1.10),
    "Tier 4: FAQs & Contacts": (4, TIER4_SERVICES_KEYWORDS, 1.00),
}

# Flat list of keywords for SentenceTransformer embedding calculation
ALL_COUNSELOR_KEYWORDS = (
    TIER1_PROGRAM_KEYWORDS +
    TIER2_ADMISSION_KEYWORDS +
    TIER3_FACULTY_KEYWORDS +
    TIER4_SERVICES_KEYWORDS
)

# Atomic keywords for Crawl4AI BestFirstCrawling Strategy native URL scorer
CRAWL4AI_SCORER_KEYWORDS = [
    "admissions", "admission", "eligibility", "criteria", "requirements",
    "apply", "fee", "fees", "tuition", "scholarship", "financial",
    "merit", "entrytest", "test", "program", "programs", "degree",
    "bs", "ms", "phd", "bachelor", "master", "undergraduate", "postgraduate",
    "courses", "department", "faculty", "prospectus", "faq", "hostel", "2026"
]

# Any plausible academic year appearing in a URL or anchor text. Recency is scored
# by comparing against the current year at runtime rather than against a hardcoded
# window: the previous CURRENT_YEAR_REGEX = r'202[4-7]' classified fall-2024 as
# "up-to-date" and ranked it first out of 287 links during a 2026 run, and the
# OUTDATED window stopped at 2023 so 2024 could never be penalised.
YEAR_TOKEN_REGEX = re.compile(r"\b(20[0-3][0-9])\b")

# Markers of an open-ended intake range ("fall 2025 onward"), whose start year is
# historical but whose content is still current.
OPEN_ENDED_YEAR_REGEX = re.compile(
    r"\b(onward|onwards|and[-\s]?onward|present|current|to[-\s]?date)\b", re.IGNORECASE
)

# How many years back an open-ended range still counts as evidence of currency.
OPEN_ENDED_GRACE_YEARS = 2

# Links scoring below threshold * this ratio are discarded as noise; those between
# it and the threshold are kept as a per-tier reserve for quota backfill.
RESERVE_FLOOR_RATIO = 0.85

# Regex patterns for stripping year/intake suffixes during canonical degree deduplication
YEAR_SUFFIX_REGEX = re.compile(
    r'-(for-)?(fall|spring)?-?(20[0-9]{2})(-(to|and|-|onward|onwards|prior|entries|entry)*)?-?(20[0-9]{2})?(-onward|-onwards|-entries|-entry)?',
    re.IGNORECASE
)


# ------------------------------------------------------------------------------
# 3b. URL SANITISATION, TOKENISED EXCLUSION & RECENCY DECAY
# ------------------------------------------------------------------------------

# Degree-level markers, used to key deduplication. A BS and an MS in the same
# discipline are different programmes and must never collapse into one another.
DEGREE_LEVEL_TOKENS = {
    "undergraduate": {
        "bs", "bsc", "bsce", "bscs", "bachelor", "bachelors", "be", "bba",
        "ba", "bfa", "bed", "bds", "mbbs", "pharmd", "llb", "undergraduate", "ug",
    },
    "graduate": {
        "ms", "msc", "mba", "mphil", "ma", "me", "mfa", "med", "llm",
        "master", "masters", "graduate", "pgd",
    },
    "postgraduate_phd": {"phd", "doctorate", "doctoral", "postdoc", "dsc", "md"},
}

# Tokens that carry no discipline meaning and must be dropped before building a
# deduplication key, otherwise /programs/bs-cs and /admissions/bs-cs look distinct.
DEDUP_STOPWORDS = {
    "program", "programs", "programme", "programmes", "degree", "degrees",
    "in", "of", "the", "and", "for", "a", "an", "with",
    "admission", "admissions", "academics", "academic", "study", "studies",
    "department", "dept", "school", "faculty", "institute", "centre", "center",
    "fall", "spring", "summer", "autumn", "onward", "onwards", "entries", "entry",
    "index", "html", "htm", "php", "aspx", "page", "detail", "details", "home",
}

PATH_TOKEN_SPLIT_REGEX = re.compile(r"[/_.\-\s]+")

# Regex constants for URL normalization and text processing
MULTI_SLASH_REGEX = re.compile(r"/{2,}")
SLUGIFY_REGEX = re.compile(r"[^a-z0-9]+")
PATH_SEPARATOR_REGEX = re.compile(r"[-_/#?=&.]")


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


def get_discipline_tokens(url: str, text: str = "") -> Tuple[str, Tuple[str, ...]]:
    """
    Build an exact deduplication key: (degree_level, sorted discipline tokens).

    Replaces the previous SequenceMatcher(ratio > 0.88) fuzzy merge, which was
    both O(n^2) over every link pair and wrong: 'bs-electrical-engineering' and
    'bs-electronic-engineering' score ~0.90 similar and were silently merged into
    a single programme. Exact token-set equality keeps them separate, while still
    merging the same programme mirrored across campus subdomains (the host is not
    part of the key) and across intake years (years are stopworded out).
    """
    parsed = urlparse(url)
    tokens = _tokenize_path(parsed.path) + _tokenize_path(text)

    level = "unspecified"
    for lvl, markers in DEGREE_LEVEL_TOKENS.items():
        if any(t in markers for t in tokens):
            level = lvl
            break

    all_level_markers = set().union(*DEGREE_LEVEL_TOKENS.values())
    discipline = {
        t for t in tokens
        if t not in DEDUP_STOPWORDS
        and t not in all_level_markers
        and not t.isdigit()
        and len(t) > 1
    }
    return level, tuple(sorted(discipline))


def allocate_proportional_tier_quotas(
    scored_links: List[Dict[str, str]],
    total_cap: int,
    shares: Optional[Dict[int, float]] = None,
) -> List[Dict[str, str]]:
    """
    Select `total_cap` links with a guaranteed floor per priority tier.

    The previous `scored_links[:max_links]` ran after a tier-major sort, so Tier 1
    consumed the entire budget and Tiers 3 and 4 contributed zero sources. Phase 3
    then asked a notebook containing no faculty or contact pages to answer the
    faculties and contact queries. Unfilled tier quota is redistributed by score
    so a small site still fills its budget.
    """
    if total_cap <= 0 or not scored_links:
        return []

    shares = shares or config.tier_quota_shares
    quotas = {t: int(total_cap * s) for t, s in shares.items()}
    remainder = total_cap - sum(quotas.values())
    if remainder > 0 and quotas:
        quotas[min(quotas)] += remainder

    by_tier: Dict[int, List[Dict[str, str]]] = {t: [] for t in quotas}
    for item in scored_links:
        by_tier.setdefault(item["priority_tier_num"], []).append(item)
    for tier in by_tier:
        # Above-threshold links first, then by score. Items carry passed_threshold
        # from classify_and_score_links; absent key means "passed" (legacy callers).
        by_tier[tier].sort(key=lambda x: (not x.get("passed_threshold", True), -x["weighted_score"]))

    selected: List[Dict[str, str]] = []
    leftovers: List[Dict[str, str]] = []
    for tier, quota in sorted(quotas.items()):
        pool = by_tier.get(tier, [])
        # A tier fills its own quota from its own reserve before any cross-tier
        # redistribution. Otherwise a global threshold tuned for Tier 1 density
        # silently starves Tier 3: a live NUST run at threshold 0.68 left only 4
        # faculty sources of a 9 quota, and the faculties query degrades with it.
        selected.extend(pool[:quota])
        leftovers.extend(pool[quota:])

    # Redistribute any genuinely unused quota to the best remaining links, again
    # preferring above-threshold candidates over any tier's reserve.
    shortfall = total_cap - len(selected)
    if shortfall > 0:
        leftovers.sort(key=lambda x: (not x.get("passed_threshold", True), -x["weighted_score"]))
        selected.extend(leftovers[:shortfall])

    selected.sort(key=lambda x: (x["priority_tier_num"], -x["weighted_score"]))
    filled = {t: sum(1 for i in selected if i["priority_tier_num"] == t) for t in sorted(quotas)}
    logger.info(f"Tier quota allocation (cap={total_cap}): {filled}")
    return selected


def slugify_university(name: str, url: str = "") -> str:
    """Stable filesystem-safe identifier used for per-university partitioned output."""
    host = urlparse(url).netloc.lower().replace("www.", "") if url else ""
    base = host.split(".")[0] if host else name
    slug = SLUGIFY_REGEX.sub("-", base.lower()).strip("-")
    return slug or "university"


# ------------------------------------------------------------------------------
# 4. HEC RECOGNIZED UNIVERSITIES DISCOVERY MODULE
# ------------------------------------------------------------------------------

HEC_RECOGNIZED_FALLBACK = [
    {"name": "National University of Sciences and Technology (NUST)", "url": "https://nust.edu.pk/", "sector": "Public", "city": "Islamabad"},
    {"name": "Lahore University of Management Sciences (LUMS)", "url": "https://lums.edu.pk/", "sector": "Private", "city": "Lahore"},
    {"name": "National University of Computer and Emerging Sciences (FAST-NUCES)", "url": "https://nu.edu.pk/", "sector": "Private", "city": "Multi-Campus"},
    {"name": "Information Technology University (ITU)", "url": "https://itu.edu.pk/", "sector": "Public", "city": "Lahore"},
    {"name": "COMSATS University Islamabad (CUI)", "url": "https://www.comsats.edu.pk/", "sector": "Public", "city": "Islamabad"},
    {"name": "Quaid-i-Azam University (QAU)", "url": "https://qau.edu.pk/", "sector": "Public", "city": "Islamabad"},
    {"name": "University of Engineering and Technology (UET) Lahore", "url": "https://uet.edu.pk/", "sector": "Public", "city": "Lahore"},
    {"name": "Aga Khan University (AKU)", "url": "https://www.aku.edu/", "sector": "Private", "city": "Karachi"},
    {"name": "Institute of Business Administration (IBA) Karachi", "url": "https://www.iba.edu.pk/", "sector": "Public", "city": "Karachi"},
    {"name": "University of the Punjab (PU)", "url": "https://pu.edu.pk/", "sector": "Public", "city": "Lahore"},
    {"name": "University of Agriculture Faisalabad (UAF)", "url": "http://uaf.edu.pk/", "sector": "Public", "city": "Faisalabad"},
    {"name": "Ghulam Ishaq Khan Institute (GIKI)", "url": "https://giki.edu.pk/", "sector": "Private", "city": "Topi"},
    {"name": "Air University Islamabad", "url": "https://www.au.edu.pk/", "sector": "Public", "city": "Islamabad"},
    {"name": "Bahria University Islamabad", "url": "https://bahria.edu.pk/", "sector": "Public", "city": "Islamabad"},
    {"name": "Government College University (GCU) Lahore", "url": "https://gcu.edu.pk/", "sector": "Public", "city": "Lahore"}
]


def extract_hec_universities(limit: int = 5) -> List[Dict[str, str]]:
    """
    Fetches official HEC-recognized Pakistani universities.
    Tries live scraping from official HEC portals, falling back gracefully to the curated top list.
    """
    logger.info(f"Retrieving top {limit} official HEC-recognized Pakistani universities...")
    hec_url = "https://www.hec.gov.pk/english/universities/pages/recognised-uk.aspx"
    
    extracted_unis = []
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(hec_url, headers=headers, timeout=8)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                text = a.get_text(strip=True)
                if href.startswith("http") and ("edu.pk" in href or "edu" in href) and text:
                    if not any(u["url"] == href for u in extracted_unis):
                        extracted_unis.append({
                            "name": text,
                            "url": href,
                            "sector": "Recognized",
                            "city": "Pakistan"
                        })
                        if len(extracted_unis) >= limit:
                            break
    except Exception as e:
        logger.warning(f"Live HEC directory scrape encountered issue: {e}. Utilizing fallback HEC database.")

    if len(extracted_unis) < limit:
        for uni in HEC_RECOGNIZED_FALLBACK:
            if not any(u["url"].lower().rstrip("/") == uni["url"].lower().rstrip("/") for u in extracted_unis):
                extracted_unis.append(uni)
                if len(extracted_unis) >= limit:
                    break

    logger.info(f"Successfully selected {len(extracted_unis)} HEC-recognized universities for processing.")
    return extracted_unis[:limit]


# ------------------------------------------------------------------------------
# 5. CRAWL4AI DEEP LINK EXTRACTION ENGINE
# ------------------------------------------------------------------------------

class CrawlFailure(RuntimeError):
    """Raised when a site could not be crawled at all, so callers can mark state."""


# --- Shared headless browser pool -------------------------------------------
#
# A browser was previously launched and torn down inside every crawl_site_links
# call. Across an 83-university batch that is 83 Chromium starts, and any crawl
# that raised before __aexit__ left the process orphaned -- the leak the plan
# targets. One browser is now started on first use and reused, with page
# recycling capping resident memory no matter how long the batch runs.

_SHARED_CRAWLER: Optional[AsyncWebCrawler] = None
_SHARED_CRAWLER_LOOP: Optional[Any] = None


def build_browser_config() -> BrowserConfig:
    """Memory-bounded browser settings for link discovery."""
    return BrowserConfig(
        headless=config.crawler_headless,
        text_mode=config.crawler_text_mode,
        light_mode=config.crawler_light_mode,
        memory_saving_mode=config.crawler_memory_saving_mode,
        max_pages_before_recycle=config.crawler_max_pages_before_recycle,
        viewport_width=config.crawler_viewport_width,
        viewport_height=config.crawler_viewport_height,
        verbose=False,
    )


async def get_shared_crawler() -> AsyncWebCrawler:
    """
    Return the process-wide crawler, starting it on first use.

    Rebinds if the running event loop changed: a browser started under a previous
    asyncio.run() holds transports attached to a now-closed loop and every call
    against it would fail.
    """
    global _SHARED_CRAWLER, _SHARED_CRAWLER_LOOP

    loop = asyncio.get_running_loop()
    if _SHARED_CRAWLER is not None and _SHARED_CRAWLER_LOOP is loop:
        return _SHARED_CRAWLER

    if _SHARED_CRAWLER is not None:
        await close_shared_crawler()

    crawler = AsyncWebCrawler(config=build_browser_config())
    await crawler.start()
    _SHARED_CRAWLER, _SHARED_CRAWLER_LOOP = crawler, loop
    logger.info("Started shared headless browser (reused across universities).")
    return crawler


async def close_shared_crawler() -> None:
    """
    Shut the shared browser down. Safe to call repeatedly and when none is open.

    The globals are cleared before awaiting close() so that a hang or error in
    teardown cannot leave a half-dead crawler installed as the shared instance.
    """
    global _SHARED_CRAWLER, _SHARED_CRAWLER_LOOP

    crawler, _SHARED_CRAWLER, _SHARED_CRAWLER_LOOP = _SHARED_CRAWLER, None, None
    if crawler is None:
        return
    try:
        await crawler.close()
        logger.info("Closed shared headless browser.")
    except Exception as e:
        logger.warning(f"Shared browser did not close cleanly: {e}")


@asynccontextmanager
async def browser_pool():
    """Scope the shared browser to a block, guaranteeing teardown on exit."""
    try:
        yield await get_shared_crawler()
    finally:
        await close_shared_crawler()


@asynccontextmanager
async def _crawler_scope():
    """
    Yield the shared crawler, or a private one when reuse is disabled.

    A private crawler is always closed here; the shared one deliberately outlives
    the block and is closed by the batch driver.
    """
    if config.crawler_reuse_browser:
        yield await get_shared_crawler()
        return

    crawler = AsyncWebCrawler(config=build_browser_config())
    await crawler.start()
    try:
        yield crawler
    finally:
        try:
            await crawler.close()
        except Exception as e:
            logger.warning(f"Per-run browser did not close cleanly: {e}")


async def crawl_site_links(start_url: str, max_pages: int = 15) -> List[Dict[str, str]]:
    """
    Uses Crawl4AI BestFirstCrawlingStrategy with KeywordRelevanceScorer
    to discover internal and external links across high-relevance pages.
    Automatically retries with alternative URL candidates (e.g. https:// vs http://)
    if the initial URL times out or fails.
    """
    # Build candidate URLs to try in priority order (http vs https, www vs non-www)
    candidates = [start_url]
    parsed_start = urlparse(start_url)

    if parsed_start.scheme == "http":
        https_url = urlunparse(("https", parsed_start.netloc, parsed_start.path, parsed_start.params, parsed_start.query, parsed_start.fragment))
        candidates.append(https_url)
    elif parsed_start.scheme == "https":
        http_url = urlunparse(("http", parsed_start.netloc, parsed_start.path, parsed_start.params, parsed_start.query, parsed_start.fragment))
        candidates.append(http_url)

    for candidate in list(candidates):
        c_parsed = urlparse(candidate)
        if c_parsed.netloc.startswith("www."):
            non_www = urlunparse((c_parsed.scheme, c_parsed.netloc[4:], c_parsed.path, c_parsed.params, c_parsed.query, c_parsed.fragment))
            if non_www not in candidates:
                candidates.append(non_www)
        else:
            with_www = urlunparse((c_parsed.scheme, f"www.{c_parsed.netloc}", c_parsed.path, c_parsed.params, c_parsed.query, c_parsed.fragment))
            if with_www not in candidates:
                candidates.append(with_www)

    last_error: Optional[str] = None

    for target_candidate in candidates:
        run_config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, score_links=True)
        scorer = KeywordRelevanceScorer(keywords=CRAWL4AI_SCORER_KEYWORDS, weight=1.0)
        strategy = BestFirstCrawlingStrategy(
            max_depth=2,
            max_pages=max_pages,
            url_scorer=scorer
        )

        aggregated_links = []
        seen_raw_hrefs = set()
        visited_count = 0
        successful_pages = 0
        crawl_error: Optional[str] = None

        async with _crawler_scope() as crawler:
            logger.info(f"Starting Crawl4AI BestFirstCrawlingStrategy for {target_candidate} (max_pages={max_pages})...")
            try:
                results = await strategy.arun(start_url=target_candidate, crawler=crawler, config=run_config)
                for res in results:
                    visited_count += 1
                    if not res.success:
                        logger.warning(f"Page failed: {getattr(res, 'url', '?')} -> {getattr(res, 'error_message', 'unknown error')}")
                        continue
                    successful_pages += 1
                    if not res.links:
                        continue

                    internal_links = res.links.get("internal", [])
                    external_links = res.links.get("external", [])
                    page_links = internal_links + external_links

                    logger.debug(f"Page {visited_count}/{max_pages}: {res.url} -> Found {len(page_links)} raw links")

                    for link in page_links:
                        href = link.get("href", "").strip()
                        if not href:
                            continue

                        if not href.lower().startswith(("http://", "https://")):
                            href = urljoin(res.url, href)

                        # Filter out document extensions before adding
                        parsed_href = urlparse(href)
                        ext = os.path.splitext(parsed_href.path)[1].lower()
                        if ext in EXCLUDED_EXTENSIONS:
                            continue

                        href_norm = href.rstrip("/").split("#")[0]
                        if href_norm not in seen_raw_hrefs:
                            seen_raw_hrefs.add(href_norm)
                            aggregated_links.append({
                                "href": href,
                                "text": link.get("text", "").strip(),
                                "title": link.get("title", "").strip(),
                                "crawl_score": link.get("total_score") or link.get("intrinsic_score"),
                            })
            except Exception as e:
                crawl_error = f"{type(e).__name__}: {e}"
                logger.error(f"Error during Crawl4AI execution for {target_candidate}: {crawl_error}")

        if aggregated_links:
            logger.info(
                f"Completed site crawl for {target_candidate}: visited {visited_count} pages "
                f"({successful_pages} succeeded), harvested {len(aggregated_links)} raw unique links."
            )
            return aggregated_links
        else:
            last_error = crawl_error or f"0 links from {target_candidate}"
            logger.warning(f"Candidate URL {target_candidate} yielded 0 links ({last_error}). Trying next candidate URL...")

    raise CrawlFailure(
        f"Crawl of {start_url} (and candidates {candidates}) produced zero links: {last_error}"
    )


# ------------------------------------------------------------------------------
# 6. ZERO GARBAGE PREPROCESSING, NOISE & KEYWORD EXCLUDE FILTER
# ------------------------------------------------------------------------------

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


# ------------------------------------------------------------------------------
# 7. CANONICAL DEGREE DEDUPLICATION POST-PROCESSING
# ------------------------------------------------------------------------------

def deduplicate_canonical_degree_links(scored_results: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Eliminate redundant programme variations using an exact structured key.

    Group key is (degree_level, sorted discipline tokens) from get_discipline_tokens.
    Because the host is not part of the key, the same BS mirrored on seecs./mcs./
    ceme. subdomains collapses to one; because years are stopworded, fall-2024 and
    fall-2025-onward collapse to one, and the recency-weighted score picks the
    survivor. Because the match is exact rather than a similarity ratio, distinct
    programmes with near-identical slugs (electrical vs electronic engineering) are
    preserved. This is O(n) with a dict instead of the previous O(n^2) pairwise
    SequenceMatcher scan (~2M comparisons at 287 links, ~41M at 60 universities).
    """
    if not scored_results:
        return []

    logger.info(f"Running Post-Processing: Canonical Degree Deduplication on {len(scored_results)} links...")

    best_by_key: Dict[Tuple, Dict[str, str]] = {}
    passthrough: List[Dict[str, str]] = []

    for item in scored_results:
        # Only programme-bearing tiers are deduplicated. A faculty index page and
        # a contact page share no discipline tokens and must not be merged.
        if item["priority_tier_num"] not in (1, 2):
            passthrough.append(item)
            continue

        level, disciplines = get_discipline_tokens(item["href"], item.get("text", ""))
        if not disciplines:
            # No discipline signal at all -- not a programme page, keep as-is.
            passthrough.append(item)
            continue

        key = (item["priority_tier_num"], level, disciplines)
        incumbent = best_by_key.get(key)
        if incumbent is None:
            best_by_key[key] = item
        elif item["weighted_score"] > incumbent["weighted_score"]:
            logger.debug(f"Replaced duplicate degree variant: '{incumbent['href']}' -> '{item['href']}'")
            best_by_key[key] = item

    final_deduped = passthrough + list(best_by_key.values())
    final_deduped.sort(key=lambda x: (x["priority_tier_num"], -x["weighted_score"]))

    logger.info(f"Canonical Deduplication complete: Reduced {len(scored_results)} links to {len(final_deduped)} distinct, non-redundant degree links.")
    return final_deduped


# ------------------------------------------------------------------------------
# 8. RAM-OPTIMIZED SEMANTIC SCORING, UP-TO-DATE FILTERING & PRIORITY CLASSIFICATION
# ------------------------------------------------------------------------------

_EMBEDDING_MODEL: Optional[SentenceTransformer] = None


def _get_embedding_model() -> SentenceTransformer:
    """
    Process-wide lazy singleton for the sentence encoder.

    The previous code constructed SentenceTransformer inside the per-university
    scoring function, so an --hec batch of 83 universities paid the model load
    83 times. The weights are stateless across calls; one instance is correct.
    """
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is None:
        logger.info("Loading SentenceTransformer ('BAAI/bge-small-en-v1.5') [once per process]...")
        _EMBEDDING_MODEL = SentenceTransformer("BAAI/bge-small-en-v1.5")
    return _EMBEDDING_MODEL


def classify_and_score_links(
    links: List[Dict[str, str]],
    threshold: float = 0.45,
    uptodate: bool = True
) -> List[Dict[str, str]]:
    """
    Computes cosine similarity between clean link text representation and counselor keywords
    using SentenceTransformer ('BAAI/bge-small-en-v1.5').
    Applies 2026 recency weighting when uptodate=True (boosts 2025-2027, penalizes 2010-2023).
    Ranks links by Priority Tier and weighted similarity score. RAM-optimized.
    """
    if not links:
        return []

    model = _get_embedding_model()

    link_texts = [
        f"{link['text']} {link['path_words']}".strip() for link in links
    ]

    # BAAI/bge-* are asymmetric: the retrieval instruction goes on the QUERY side
    # only. Here the counselor keywords are the queries and the links are the
    # passages, so the prefix is applied to the keywords and never to the links.
    # Encoding both sides bare (the previous behaviour) collapses the score spread
    # the --threshold was tuned against.
    prefixed_keywords = [f"{config.bge_query_prefix}{k}" for k in ALL_COUNSELOR_KEYWORDS]

    logger.info(f"Encoding {len(ALL_COUNSELOR_KEYWORDS)} keywords and {len(link_texts)} links (uptodate={uptodate})...")
    keyword_embeddings = model.encode(prefixed_keywords, convert_to_tensor=True, normalize_embeddings=True)
    link_embeddings = model.encode(
        link_texts, convert_to_tensor=True, normalize_embeddings=True, batch_size=64
    )

    similarity_matrix = model.similarity(link_embeddings, keyword_embeddings)

    scored_results = []
    for idx, link in enumerate(links):
        scores = similarity_matrix[idx]
        max_score = float(scores.max())
        best_keyword_idx = int(scores.argmax())
        matched_keyword = ALL_COUNSELOR_KEYWORDS[best_keyword_idx]

        # Sub-threshold links are retained but flagged, not dropped. They form each
        # tier's reserve so that a threshold tuned for the dense programme tier
        # cannot starve the sparse faculties/contacts tiers. Anything below the
        # hard floor is genuine noise and is discarded outright.
        passed = max_score >= threshold
        if max_score < threshold * RESERVE_FLOOR_RATIO:
            continue

        assigned_tier = "Tier 4: FAQs & Contacts"
        tier_weight = 1.00
        tier_num = 4

        for tier_name, (t_num, t_keywords, t_weight) in PRIORITY_TIERS.items():
            if matched_keyword in t_keywords:
                assigned_tier = tier_name
                tier_num = t_num
                tier_weight = t_weight
                break

        recency_factor = 1.00
        year_tag = "Current / Timeless"

        if uptodate:
            combined_str = f"{link['href']} {link['text']} {link['path_words']}"
            recency_factor, year_tag = compute_year_decay_factor(combined_str)

        weighted_score = round(max_score * tier_weight * recency_factor, 4)

        scored_results.append({
            "href": link["href"],
            "text": link["text"],
            "raw_text": link["raw_text"],
            "matched_keyword": matched_keyword,
            "raw_similarity_score": round(max_score, 4),
            "weighted_score": weighted_score,
            "category": assigned_tier,
            "priority_tier_num": tier_num,
            "year_tag": year_tag,
            "passed_threshold": passed,
        })

    # The model itself is a process-wide singleton and is intentionally NOT freed:
    # reloading BAAI/bge-small-en-v1.5 per university cost ~3-5s x 83 universities
    # per batch run for no benefit. Only the per-run tensors are released.
    del keyword_embeddings, link_embeddings, similarity_matrix
    gc.collect()

    scored_results.sort(key=lambda x: (x["priority_tier_num"], -x["weighted_score"]))
    n_passed = sum(1 for r in scored_results if r["passed_threshold"])
    logger.info(
        f"Scoring complete: {n_passed} links passed quality threshold ({threshold}); "
        f"{len(scored_results) - n_passed} retained as tier reserve."
    )

    deduped_results = deduplicate_canonical_degree_links(scored_results)
    return deduped_results


# ------------------------------------------------------------------------------
# 9. DUAL OUTPUT GENERATORS
# ------------------------------------------------------------------------------

def export_dual_outputs(
    results: List[Dict[str, str]],
    output_links_path: str = "extracted_links.txt",
    output_detailed_path: str = "extracted_links_detailed.txt",
    university_name: str = "Target University",
    uptodate: bool = True,
    exclude_keywords: str = "news|events"
):
    """
    Generates two distinct files:
    1. extracted_links.txt -> Clean list of canonical URLs only (one per line)
    2. extracted_links_detailed.txt -> Full structured breakdown per link
    """
    out_links = Path(output_links_path).resolve()
    out_detailed = Path(output_detailed_path).resolve()

    urls_only = [item["href"] for item in results]
    with open(out_links, "w", encoding="utf-8") as f:
        f.write("\n".join(urls_only) + ("\n" if urls_only else ""))
    logger.info(f"Exported {len(urls_only)} canonical quality links to '{out_links}'")

    with open(out_detailed, "w", encoding="utf-8") as f:
        f.write("=" * 100 + "\n")
        f.write(f"EDUCATION COUNSELING RAG - HIGH QUALITY EXTRACTED LINKS REPORT\n")
        f.write(f"University / Source         : {university_name}\n")
        f.write(f"Up-To-Date (2026) Filtering : {'ENABLED (Prioritizing 2026)' if uptodate else 'DISABLED (All Years)'}\n")
        f.write(f"Excluded Patterns Filter    : {exclude_keywords}\n")
        f.write(f"Canonical Degree Dedup      : ENABLED (Unique Programs Only)\n")
        f.write(f"Total Quality Links Extracted: {len(results)}\n")
        f.write("=" * 100 + "\n\n")

        current_category = ""
        for idx, item in enumerate(results, start=1):
            if item["category"] != current_category:
                current_category = item["category"]
                f.write(f"\n{'#' * 80}\n")
                f.write(f" CATEGORY: {current_category.upper()}\n")
                f.write(f"{'#' * 80}\n\n")

            f.write(f"[{idx:03d}] URL: {item['href']}\n")
            f.write(f"      Anchor Text      : {item['text']}\n")
            f.write(f"      Year Tag Status  : {item.get('year_tag', 'N/A')}\n")
            f.write(f"      Raw Similarity   : {item['raw_similarity_score'] * 100:.1f}%\n")
            f.write(f"      Weighted Score   : {item['weighted_score']:.4f}\n")
            f.write(f"      Matched Keyword  : {item['matched_keyword']}\n")
            f.write("-" * 100 + "\n")

    logger.info(f"Exported detailed extraction report to '{out_detailed}'")


def export_partitioned_links(
    results: List[Dict[str, str]],
    uni_slug: str,
    uni_name: str,
    uni_url: str,
) -> Path:
    """
    Write one JSONL file per university to data/links/<slug>.jsonl.

    This is the Phase 2 contract. The single shared extracted_links.txt cannot
    satisfy it: an --hec batch run wrote 287 undifferentiated links of which ~284
    were NUST, ~10 were LUMS and 0 were ITU, with nothing in the file recording
    which university a given URL belonged to. Phase 2 provisions one notebook per
    university and therefore needs the partition, plus the tier of each URL so
    Phase 3 can scope its queries with source_ids.
    """
    config.data_links_dir.mkdir(parents=True, exist_ok=True)
    out_path = config.data_links_dir / f"{uni_slug}.jsonl"
    # Streamed record-by-record into a temp file, then renamed atomically. A
    # crash mid-write previously left a truncated .jsonl that Phase 2 would
    # ingest as if it were the complete link partition.
    tmp_path = out_path.with_name(out_path.name + ".tmp")

    with open(tmp_path, "w", encoding="utf-8") as f:
        for rank, item in enumerate(results, start=1):
            f.write(json.dumps({
                "university_slug": uni_slug,
                "university_name": uni_name,
                "university_url": uni_url,
                "rank": rank,
                "url": item["href"],
                "anchor_text": item["text"],
                "tier": item["priority_tier_num"],
                "tier_name": item["category"],
                "weighted_score": item["weighted_score"],
                "raw_similarity_score": item["raw_similarity_score"],
                "matched_keyword": item["matched_keyword"],
                "year_tag": item.get("year_tag", "N/A"),
            }, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, out_path)
    logger.info(f"Exported {len(results)} partitioned links to '{out_path}'")
    return out_path


def load_partitioned_links(uni_slug: str) -> List[Dict[str, object]]:
    """Read back a per-university link partition written by export_partitioned_links."""
    path = config.data_links_dir / f"{uni_slug}.jsonl"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ------------------------------------------------------------------------------
# 10. MAIN WORKFLOW CONTROLLER
# ------------------------------------------------------------------------------

async def run_pipeline(
    url: str = None,
    hec_mode: bool = False,
    hec_limit: int = 5,
    max_links: int = 100,
    threshold: float = 0.45,
    max_pages: int = 15,
    uptodate: bool = True,
    exclude_keywords: str = "news|events",
    output_links: str = "extracted_links.txt",
    output_detailed: str = "extracted_links_detailed.txt"
):
    """
    Main orchestration function managing single site or HEC batch processing.
    """
    targets = []
    
    if hec_mode:
        logger.info(f"--- Running in HEC Recognized Universities Batch Mode (Limit={hec_limit}) ---")
        hec_unis = extract_hec_universities(limit=hec_limit)
        for u in hec_unis:
            targets.append((u["name"], u["url"]))
    else:
        target_url = url if url else "https://itu.edu.pk/admissions/"
        targets.append(("Target University", target_url))

    all_processed_results = []
    succeeded: List[Tuple[str, str, int]] = []
    failed: List[Tuple[str, str]] = []

    for uni_name, target_url in targets:
        uni_slug = slugify_university(uni_name, target_url)
        logger.info(f"\n==========================================================================")
        logger.info(f" Processing: {uni_name} [{uni_slug}] ({target_url}) [UpToDate={uptodate}] [Exclude='{exclude_keywords}']")
        logger.info(f"==========================================================================")

        try:
            raw_links = await crawl_site_links(start_url=target_url, max_pages=max_pages)
            clean_links = preprocess_and_filter_links(raw_links, base_url=target_url, exclude_keywords=exclude_keywords)
            scored_links = classify_and_score_links(clean_links, threshold=threshold, uptodate=uptodate)

            # Dynamic 45% link selection strategy (capped at 150 max_links)
            candidate_count = len(scored_links)
            dynamic_target = min(max(15, int(candidate_count * 0.45)), max_links)
            logger.info(f"Dynamic Link Allocation: Discovered {candidate_count} scored links -> selecting {dynamic_target} links (45% ratio, cap={max_links}).")

            # Tier-proportional selection, not a flat top-N slice.
            top_quality_links = allocate_proportional_tier_quotas(scored_links, total_cap=dynamic_target)


            if not top_quality_links:
                raise CrawlFailure(
                    f"No links survived filtering/scoring for {uni_name} "
                    f"(raw={len(raw_links)}, clean={len(clean_links)}, scored={len(scored_links)}, "
                    f"threshold={threshold})"
                )

            export_partitioned_links(top_quality_links, uni_slug, uni_name, target_url)
            all_processed_results.extend(top_quality_links)
            succeeded.append((uni_name, uni_slug, len(top_quality_links)))
            logger.info(f"Retained {len(top_quality_links)} canonical quality links for {uni_name} (cap={max_links}).")

        except Exception as e:
            # One bad site must not abort an 83-university batch, but it must also
            # never be reported as a success.
            failed.append((uni_name, f"{type(e).__name__}: {e}"))
            logger.error(f"FAILED {uni_name} ({target_url}): {e}")

    export_dual_outputs(
        results=all_processed_results,
        output_links_path=output_links,
        output_detailed_path=output_detailed,
        university_name="HEC Universities Batch" if hec_mode else targets[0][0],
        uptodate=uptodate,
        exclude_keywords=exclude_keywords
    )

    print("\n" + "=" * 80)
    if failed and not succeeded:
        print(f" FAILED: all {len(failed)} target(s) produced zero links.")
    elif failed:
        print(f" PARTIAL: {len(succeeded)} of {len(targets)} universities succeeded, {len(failed)} failed.")
    else:
        print(f" SUCCESS: {len(succeeded)} of {len(targets)} universities processed.")
    print(f" -> Total canonical links: {len(all_processed_results)}")
    for name, slug, count in succeeded:
        print(f"    [ok]   {name}: {count} links -> data/links/{slug}.jsonl")
    for name, err in failed:
        print(f"    [FAIL] {name}: {err}")
    print(f" -> Up-To-Date (2026)   : {'ENABLED' if uptodate else 'DISABLED'}")
    print(f" -> Exclude Keywords    : {exclude_keywords}")
    print(f" -> Plain Links File    : {Path(output_links).resolve()}")
    print(f" -> Detailed Info File  : {Path(output_detailed).resolve()}")
    print("=" * 80 + "\n")

    if failed and not succeeded:
        sys.exit(1)

    return {"succeeded": succeeded, "failed": failed, "links": all_processed_results}


# ------------------------------------------------------------------------------
# 11. CLI ENTRY POINT
# ------------------------------------------------------------------------------

def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected (true/false).')


def main():
    parser = argparse.ArgumentParser(
        description="HEC Recognized University & Education Counselor Link Extractor (Phase 1)"
    )
    parser.add_argument(
        "--url",
        type=str,
        default=None,
        help="Target university webpage URL to crawl (e.g. https://nust.edu.pk)."
    )
    parser.add_argument(
        "--hec",
        action="store_true",
        help="Enable HEC directory mode to auto-discover official Pakistani universities."
    )
    parser.add_argument(
        "--hec-limit",
        type=int,
        default=5,
        help="Number of HEC recognized universities to process in batch mode (default: 5)."
    )
    parser.add_argument(
        "--max-links",
        type=int,
        default=100,
        help="Maximum number of top-quality links to extract per run/university (default: 100)."
    )
    parser.add_argument(
        "--exclude-keywords",
        type=str,
        default="news|events",
        help="Pipe-separated string of keywords/patterns to filter out and remove (default: 'news|events')."
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.45,
        help="Semantic similarity score threshold between 0.0 and 1.0 (default: 0.45)."
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=15,
        help="Maximum number of sub-pages to crawl per site (default: 15)."
    )
    parser.add_argument(
        "--uptodate",
        type=str2bool,
        nargs='?',
        const=True,
        default=True,
        help="Prioritize 2026/current academic year links and penalize historical outdated links (default: true)."
    )
    parser.add_argument(
        "--output-links",
        type=str,
        default="extracted_links.txt",
        help="Path for plain text links output file (default: extracted_links.txt)."
    )
    parser.add_argument(
        "--output-detailed",
        type=str,
        default="extracted_links_detailed.txt",
        help="Path for detailed text report output file (default: extracted_links_detailed.txt)."
    )

    args = parser.parse_args()

    async def _main() -> None:
        # run_pipeline leaves the shared browser open so a caller processing many
        # universities keeps reusing it; the CLI owns teardown for its own run.
        try:
            await run_pipeline(
                url=args.url,
                hec_mode=args.hec,
                hec_limit=args.hec_limit,
                max_links=args.max_links,
                threshold=args.threshold,
                max_pages=args.max_pages,
                uptodate=args.uptodate,
                exclude_keywords=args.exclude_keywords,
                output_links=args.output_links,
                output_detailed=args.output_detailed
            )
        finally:
            await close_shared_crawler()

    asyncio.run(_main())


if __name__ == "__main__":
    main()