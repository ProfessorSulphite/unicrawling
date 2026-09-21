"""
Autonomous discovery and classification of academic department, school, and faculty hubs.

Detects decentralized school and department portals (e.g. mitsloan.mit.edu, eecs.mit.edu,
mitadmissions.org, seecs.nust.edu.pk) from root university crawls and registry facts,
filtering them using TypeSafe Jev System One or deterministic heuristics.
"""

import asyncio
import logging
import re
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse, urlunparse

from src.extractor.linkers.constants import logger
from src.extractor.linkers.filteration import is_same_institution, registrable_domain
from src.utilities.registry import lookup
from src.utilities.typesafe_client import evaluate_noul, is_typesafe_available

# Subdomain and path prefixes that are purely administrative, operational, or technical noise
NON_ACADEMIC_SUBDOMAINS: Set[str] = {
    "mail", "email", "webmail", "vpn", "login", "auth", "sso", "idp", "admin",
    "help", "helpdesk", "it", "support", "facilities", "parking", "housing",
    "dining", "bookstore", "police", "security", "events", "news", "calendar",
    "alumni", "giving", "donations", "jobs", "careers", "hr", "payroll",
    "canvas", "moodle", "blackboard", "library", "libraries", "cpanel", "search",
    "www", "web", "media", "video", "photos", "chat", "status", "api",
}

# Regex to detect path-based department/school hubs on the primary domain
ACADEMIC_PATH_HUB_REGEX = re.compile(
    r"^((?:/(?:academics/)?(?:schools?|colleges?|facult(?:y|ies)|departments?)/[a-zA-Z0-9_-]+))(?:/.*)?$",
    re.IGNORECASE,
)

# Common academic keywords indicating a school or department
ACADEMIC_KEYWORDS = {
    "school", "dept", "department", "faculty", "college", "institute", "division",
    "admissions", "admission", "undergraduate", "graduate", "eng", "engineering",
    "sci", "science", "sciences", "med", "medical", "medicine", "law", "biz",
    "business", "management", "comp", "computing", "computer", "arts", "soc",
    "social", "env", "environment", "arch", "architecture", "econ", "economics",
    "chem", "chemistry", "bio", "biology", "phys", "physics", "math", "humanities",
    "health", "nursing", "pharmacy", "education",
}


def normalize_hub_url(url: str) -> str:
    """
    Normalize an academic hub URL to its base landing page.

    For subdomain portals (e.g. https://mitsloan.mit.edu/admissions/apply), returns 'https://mitsloan.mit.edu/'.
    For path portals (e.g. https://mit.edu/schools/engineering/overview), returns 'https://mit.edu/schools/engineering'.
    """
    parsed = urlparse(url.strip())
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower()

    path_match = ACADEMIC_PATH_HUB_REGEX.match(parsed.path)
    if path_match:
        hub_path = path_match.group(1).rstrip("/")
        return urlunparse((scheme, netloc, hub_path, "", "", ""))

    # For subdomains or root portals, return root origin
    return urlunparse((scheme, netloc, "/", "", "", ""))


def is_academic_hub_candidate(url: str, text: str = "", base_url: str = "") -> bool:
    """
    Heuristic check to determine if a URL represents an academic department or school hub.
    """
    if not url or not base_url:
        return False

    if not is_same_institution(url, base_url):
        return False

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    text_lower = (text or "").lower()

    base_reg = registrable_domain(urlparse(base_url).netloc)
    host_labels = [l for l in host.split(".") if l]

    # 1. Distinct Subdomain or Institutional Alias Hub Check (e.g., mitsloan.mit.edu, seecs.nust.edu.pk, mitadmissions.org)
    if host != base_reg:
        subdomain_prefix = host_labels[0]
        if subdomain_prefix in NON_ACADEMIC_SUBDOMAINS:
            return False

        # If anchor text explicitly contains academic keywords, high confidence
        if any(kw in text_lower for kw in ("school", "department", "faculty", "college", "admissions", "admission", "institute")):
            return True

        # If subdomain prefix matches academic tokens
        if any(kw in subdomain_prefix for kw in ACADEMIC_KEYWORDS):
            return True

        # If it's a recognized institutional alias (different domain)
        if registrable_domain(host) != base_reg:
            return True

        # Any non-noise subdomain of the university is a plausible candidate
        return True

    # 2. Path Hub Check on main domain (e.g. https://mit.edu/schools/engineering)
    if ACADEMIC_PATH_HUB_REGEX.match(path):
        return True

    # 3. Anchor Text Check for prominent school/department names
    if any(phrase in text_lower for phrase in ("school of", "department of", "faculty of", "college of", "undergraduate admissions", "graduate admissions")):
        # Ensure it's not a generic file or deep document
        if not path.endswith((".pdf", ".jpg", ".png", ".html")):
            return True

    return False


async def classify_academic_hubs_jev(candidate_hubs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Classify candidate hubs concurrently using TypeSafe Jev System One.

    Confirms whether a candidate portal is a genuine academic degree-granting unit or admissions hub,
    filtering out campus services, athletic clubs, or IT utilities.
    """
    if not candidate_hubs:
        return []

    if not is_typesafe_available():
        # Deterministic offline fallback using academic keywords
        verified = []
        for hub in candidate_hubs:
            text = (hub.get("text") or "").lower()
            url = hub.get("href", "").lower()
            if any(kw in text or kw in url for kw in ACADEMIC_KEYWORDS):
                verified.append(hub)
            elif "admissions" in url or "admissions" in text:
                verified.append(hub)
        return verified or candidate_hubs[:5]

    async def _evaluate_single(hub: Dict[str, str]) -> Optional[Dict[str, str]]:
        url = hub.get("href", "")
        text = hub.get("text", "")
        state = f"URL: {url}\nTitle/Anchor: {text}"
        instructions = (
            "Is this university web portal the official academic school, faculty, college, "
            "department, or admissions division offering degree programs or admissions curriculum, "
            "rather than a non-academic campus facility, IT utility, or student club?"
        )
        try:
            prob = await evaluate_noul(state=state, instructions=instructions)
            if prob is not None and prob >= 0.70:
                logger.debug(f"Jev confirmed academic hub: {url} (prob={prob:.2f})")
                return hub
        except Exception as e:
            logger.debug(f"Jev hub check skipped for {url}: {e}")
            # On network error, retain candidate if it passes heuristic
            if any(kw in (text + url).lower() for kw in ACADEMIC_KEYWORDS):
                return hub
        return None

    results = await asyncio.gather(*(_evaluate_single(h) for h in candidate_hubs))
    return [h for h in results if h is not None]


async def detect_academic_department_hubs(
    raw_links: List[Dict[str, str]],
    base_url: str,
    max_hubs: int = 8,
) -> List[str]:
    """
    Autonomously discover and select top academic department/school hubs for a university.

    Combines:
    1. Harvested links from the primary root crawl.
    2. Known institutional aliases from rankings_global.json registry.
    3. TypeSafe Jev System One semantic verification.
    """
    if not base_url:
        return []

    logger.info(f"Scanning harvested links for decentralized academic departments and school portals...")
    candidates: List[Dict[str, str]] = []
    seen_normalized: Set[str] = set()

    base_norm = normalize_hub_url(base_url).rstrip("/")
    seen_normalized.add(base_norm)

    # 1. Add known institutional domain aliases from registry
    try:
        base_domain = urlparse(base_url).netloc
        entry = lookup(base_domain)
        if entry:
            for alias in entry.get("aliases", []):
                alias_url = f"https://{alias}" if not alias.startswith("http") else alias
                norm = normalize_hub_url(alias_url).rstrip("/")
                if norm not in seen_normalized and is_same_institution(alias_url, base_url):
                    seen_normalized.add(norm)
                    candidates.append({
                        "href": norm,
                        "text": f"Registry Alias: {alias}",
                    })
    except Exception as e:
        logger.debug(f"Registry alias lookup for {base_url} encountered: {e}")

    # 2. Scan links discovered during the root crawl
    for link in raw_links:
        href = link.get("href", "").strip()
        text = link.get("text", "").strip()
        if not href:
            continue

        if is_academic_hub_candidate(href, text=text, base_url=base_url):
            norm = normalize_hub_url(href).rstrip("/")
            if norm not in seen_normalized:
                seen_normalized.add(norm)
                candidates.append({"href": norm, "text": text})

    if not candidates:
        logger.info("No decentralized departmental or school portals detected.")
        return []

    logger.info(f"Identified {len(candidates)} candidate academic portals. Verifying with Jev System One...")
    verified = await classify_academic_hubs_jev(candidates)

    # Deduplicate and cap to max_hubs
    selected_hubs = [h["href"] for h in verified[:max_hubs]]
    logger.info(f"Selected {len(selected_hubs)} verified academic department/school hubs for fan-out crawling: {selected_hubs}")
    return selected_hubs
