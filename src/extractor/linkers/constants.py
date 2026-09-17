"""
Shared vocabulary for Phase 1 link extraction: exclusion rules, counselor keyword
tiers, and the compiled regexes every other linkers module depends on.

Imports nothing from its siblings -- it is the bottom of this package's dependency
order (constants -> filteration -> deduplication -> semantic_scoring -> runner,
with crawling depending only on constants).

The logging setup below is an import-time side effect carried over verbatim from
extract_links.py, where it ran when that module was imported. Every module in this
package imports constants, so it still fires exactly once and at the same point in
the import graph. It belongs in a real logging setup rather than a constants
module; moving it is left for a later pass so this commit stays a pure move.
"""

import logging
import re
import sys

from pathlib import Path


# ------------------------------------------------------------------------------
# Project root, not the src/ package directory -- logs and data belong beside the
# repository, not inside the source tree. Three levels up from
# src/extractor/linkers/, where extract_links.py needed only two.
BASE_DIR = Path(__file__).resolve().parents[3]
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
    # On-site search and taxonomy pages. A search result page has no content of
    # its own -- it is a rendering of a query -- so ingesting one grounds an
    # answer in a list of titles. The 2026-09-05 COMSATS notebook spent two of
    # its 41 slots on /search.aspx?q=research and /search.aspx?q=academic+programs.
    # Safe as tokens: "research" and "researcher" are distinct tokens from
    # "search", which is the whole reason this set is token-matched.
    "search", "tag", "tags", "print", "share", "comment", "comments",
    "cart", "checkout", "unsubscribe", "captcha",
}

# Query keys that make a URL a search result rather than a page. Matched on the
# key alone: the value is the user's query and can be anything.
SEARCH_QUERY_PARAMS = {"q", "s", "query", "keyword", "keywords", "search", "term"}

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
# C32 removed "merit list closing aggregate formula 2026". Tier is assigned
# purely by which keyword wins cosine argmax, and that one phrase's embedding
# matched generic admissions language rather than merit lists specifically: it
# won 13 of ITU's 46 selected links, more than any programme keyword, and a
# merit list carries neither a fee nor a deadline. It is replaced by two
# narrower phrases aimed at the documents that DO carry them.
#
# The cost of a broad keyword here is paid twice over: it wins argmax, which
# both mis-tiers the page AND displaces a page that would have answered.
TIER2_ADMISSION_KEYWORDS = [
    "tuition fee structure semester charges 2026", "fee refund policy rules",
    "admission eligibility criteria minimum marks 2026", "online application portal apply now 2026",
    "admission schedule deadline entry test 2026",
    "application processing fee payment challan",
    "academic calendar admission dates last date to apply",
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

# ------------------------------------------------------------------------------
# 3a. STRUCTURAL TIER OVERRIDES (C32)
#
# Tier was decided entirely by which of the 23 keywords won cosine argmax --
# there was no URL rule of any kind. That gives the embedding a monopoly on a
# judgement the URL path is often far better evidence for, and it fails in both
# directions:
#
#   * A merit-list page reads like an admissions page to an embedding, because
#     it IS about admissions. It won Tier 2 on ITU 13 times and occupied slots
#     the programme and fee queries then read, finding nothing in them.
#   * ITU's /financial-assistance page landed in Tier 4, which NO programme
#     query reads at all, because "scholarships" matched a Tier-4 phrase first.
#
# These two lists give the path structure a vote the embedding cannot override.
# They are deliberately generic rather than tuned to one university: a listings
# page of outcomes (results, merit lists, notice boards, staff directories) is
# low-value corpus everywhere, and a page whose path says "fee" or "deadline"
# is high-value everywhere.

# Matched as a substring of the lowercased path. Any hit forces the link to
# Tier 3 AFTER scoring, so it can never occupy a Tier-1/2 slot that the
# programme and fee queries read. Tier 3 rather than exclusion: a merit list is
# real institutional content and still answers "what does admission look like
# here", it simply must not crowd out the pages carrying fees and deadlines.
DEMOTED_PATH_PATTERNS = (
    # Outcome listings -- published once per intake, superseded immediately,
    # and carrying no fee, no deadline and no programme description.
    "merit-list", "merit_list", "meritlist", "/merit/",
    "/result/", "/results/", "waiting-list", "selected-candidates",
    "notice-board", "noticeboard", "/notices/", "/circular",
    # Person directories. Dozens to hundreds of near-identical pages on any
    # large university site; they answer nothing the schema asks for.
    "/profile/", "/profiles/", "/people/", "/person/", "/staff/",
    "/directory/", "/team/",
    # Historical and administrative archives.
    "/alumni/", "/library/", "/gallery/",
)

# Matched the same way, but as a PROMOTION: a page whose path says it carries
# fees, deadlines or application mechanics is pinned to Tier 2 whatever the
# embedding thought, and at least one match per pattern is pulled into the
# selection if it exists anywhere in the scored set. These are exactly the
# fields the ITU run answered for 0% of programmes.
GUARANTEED_PATH_PATTERNS = (
    "fee", "financial", "scholarship", "funding", "tuition",
    "deadline", "schedule", "calendar", "important-date", "key-date",
    "prospectus", "how-to-apply", "apply-now", "admission-guide",
)

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
# Keyed by the C17 DegreeLevel names so this map and the payload taxonomy read
# the same. The keys never leave deduplication.py -- they only have to be
# distinct from one another -- but keeping one vocabulary avoids a translation
# nobody would remember to do.
#
# "pgd" moved out of the masters set into its own diploma level: under the old
# three-way split a "PGD in Data Science" link deduped against an "MS in Data
# Science" link and one of them was silently dropped. They are different
# programmes, which is the whole reason this map exists.
DEGREE_LEVEL_TOKENS = {
    "bachelors": {
        "bs", "bsc", "bsce", "bscs", "bachelor", "bachelors", "be", "bba",
        "ba", "bfa", "bed", "bds", "mbbs", "pharmd", "llb", "undergraduate", "ug",
    },
    "masters": {
        "ms", "msc", "mba", "mphil", "ma", "me", "mfa", "med", "llm",
        "master", "masters", "graduate",
    },
    # "postdoc" is kept as a marker here and nowhere else: post-doctoral is not a
    # level a student applies to (plan section 1, note 5), but a post-doc link
    # still must not collapse into a PhD link.
    "phd": {"phd", "doctorate", "doctoral", "postdoc", "dsc", "md"},
    "diploma": {"pgd", "pgdip", "diploma", "certificate"},
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
    # C32. Without these, ITU's three merit lists for the SAME programme --
    #   /merit-lists-2026/bs-software-engineering
    #   /merit-lists-2026/faculty-of-engineering/bs-software-engineering
    #   /merit-lists-2026-2/bs-software-engineering-2nd-round
    # produce three different discipline-token sets and survive deduplication as
    # three distinct "programmes". Dedup reduced 96 links to 93 on that run.
    "merit", "merits", "list", "lists", "round", "rounds", "result", "results",
    "1st", "2nd", "3rd", "4th", "5th", "first", "second", "third",
    "final", "provisional", "revised", "updated", "notice", "notification",
}

# Path segments stripped whole before tokenisation, because they describe WHERE
# a page sits rather than WHAT it is about. A programme reached via
# /faculty-of-engineering/ and the same programme reached directly must produce
# the same dedup key, or one real programme becomes two.
DEDUP_STRIPPED_SEGMENT_REGEX = re.compile(
    r"(?:^|/)(?:merit-lists?(?:-\d{4})?(?:-\d+)?"
    r"|faculty-of-[a-z-]+"
    r"|school-of-[a-z-]+"
    r"|department-of-[a-z-]+"
    r"|results?-\d{4}"
    r")(?=/|$)",
    re.IGNORECASE,
)

PATH_TOKEN_SPLIT_REGEX = re.compile(r"[/_.\-\s]+")

# Regex constants for URL normalization and text processing
MULTI_SLASH_REGEX = re.compile(r"/{2,}")
SLUGIFY_REGEX = re.compile(r"[^a-z0-9]+")
PATH_SEPARATOR_REGEX = re.compile(r"[-_/#?=&.]")
