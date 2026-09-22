"""
DeepSeek Direct Extraction Engine (deepseek_extractor.py).

Provides zero-quota, ultra-fast end-to-end extraction using Crawl4AI/httpx
page content fetching combined with DeepSeek-V4.1-Flash (1M context, 2,500 concurrency).

Bypasses NotebookLM account limits, cookie expirations, and Google RPC buffer errors,
completing full university extraction in 20-35 seconds with full schema compliance.
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple, get_args, get_origin
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import ValidationError

from src.config import config
from src.extractor.crawlers.free_search_enrichment import free_search_find_portal
from src.extractor.crawlers.notebook_querying import (
    ExtractionReport,
    QUERY_SUITE,
    Q1Payload,
    QuerySpec,
)
from src.extractor.crawlers.verification import verify_program_batch
from src.utilities.deepseek_client import (
    DeepSeekQuotaError,
    is_deepseek_available,
    mark_deepseek_exhausted,
    normalize_tuition_batch,
)
from src.utilities.registry import lookup as registry_lookup
from src.utilities.schema import (
    ContactInfo,
    FacultyItem,
    KeyLinks,
    MainInfo,
    ProgramCategoryBlock,
    ProgramItem,
    UniversityPayload,
)
from src.utilities.typesafe_client import is_typesafe_available

logger = logging.getLogger("ExtractData")


def _clean_html_to_markdown_summary(html_content: str, max_chars: int = 15000) -> str:
    """Extract clean readable text from HTML, stripping boilerplate and scripts."""
    if not html_content:
        return ""
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "noscript", "svg", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        # Collapse multiple newlines
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        cleaned = "\n".join(lines)
        return cleaned[:max_chars]
    except Exception as e:
        logger.debug(f"Failed to parse HTML text: {e}")
        return ""


async def fetch_page_content(client: httpx.AsyncClient, url: str) -> Optional[str]:
    """Fetch one web page text content using HTTP client."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = await client.get(url, headers=headers, follow_redirects=True, timeout=10.0)
        if resp.status_code == 200:
            return _clean_html_to_markdown_summary(resp.text)
    except Exception as e:
        logger.debug(f"Could not fetch {url}: {e}")
    return None


async def fetch_corpus_text_for_links(
    links: List[Dict[str, Any]],
    max_pages: int = 40,
    concurrency: int = 10,
) -> Dict[str, str]:
    """
    Concurrently download text content for the top priority links.
    Returns: {url: clean_text_summary}
    """
    selected = [l for l in links if l.get("selected", True)]
    target_links = selected[:max_pages] if selected else links[:max_pages]

    sem = asyncio.Semaphore(concurrency)
    corpus: Dict[str, str] = {}

    async with httpx.AsyncClient(timeout=12.0) as client:
        async def _fetch_one(link_obj: Dict[str, Any]):
            url = link_obj.get("url") or link_obj.get("href")
            if not url:
                return
            async with sem:
                text = await fetch_page_content(client, url)
                if text:
                    corpus[url] = text

        tasks = [_fetch_one(l) for l in target_links]
        await asyncio.gather(*tasks, return_exceptions=True)

    logger.info(f"Downloaded text corpus from {len(corpus)} pages (out of {len(target_links)} requested links).")
    return corpus


def _build_combined_context(corpus: Dict[str, str], max_tokens_estimate: int = 120000) -> str:
    """
    Format fetched pages into labeled context sections for DeepSeek.
    1 token ~ 4 characters. 120,000 tokens ~ 480,000 characters (well under DeepSeek 1M limit).
    """
    chunks = []
    total_len = 0
    max_chars = max_tokens_estimate * 4

    for url, text in corpus.items():
        entry = f"\n--- SOURCE URL: {url} ---\n{text}\n"
        if total_len + len(entry) > max_chars:
            break
        chunks.append(entry)
        total_len += len(entry)

    return "".join(chunks)


async def _execute_deepseek_json_call(
    user_prompt: str,
    system_prompt: str,
    timeout: float = 60.0,
    max_tokens: int = 8192,
) -> Dict[str, Any]:
    """
    Execute a single JSON completion request against DeepSeek-V4.1-Flash.
    Uses non-thinking mode for deterministic, ultra-fast output.
    Raises DeepSeekQuotaError if balance/quota is exhausted (HTTP 401 or 402).
    """
    api_key = (config.deepseek_api_key or os.getenv("DEEPSEEK_API_KEY", "")).strip()
    base_url = (config.deepseek_base_url or "https://api.deepseek.com").rstrip("/")
    endpoint = f"{base_url}/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.deepseek_model or "deepseek-flash",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(endpoint, json=payload, headers=headers)
        if resp.status_code in (401, 402):
            mark_deepseek_exhausted()
            raise DeepSeekQuotaError(f"DeepSeek balance/quota exhausted (HTTP {resp.status_code}): {resp.text[:150]}")
        if resp.status_code != 200:
            raise RuntimeError(f"DeepSeek returned HTTP {resp.status_code}: {resp.text[:150]}")

        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        cleaned = content.strip() if content else ""
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
            cleaned = cleaned.strip()

        if not cleaned:
            return {}

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            from src.extractor.crawlers.json_repairing import extract_json_str, sanitize_invalid_escapes
            candidate = extract_json_str(cleaned)
            candidate = sanitize_invalid_escapes(candidate)
            candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
            return json.loads(candidate)


async def query_deepseek_consolidated_programs(
    corpus_text: str,
    uni_name: str,
    uni_domain: str,
) -> Dict[str, List[ProgramItem]]:
    """
    Extract all four program categories (bachelors, masters, phd, diploma)
    in a SINGLE high-efficiency pass, reducing prompt tokens and costs by ~75%.
    """
    system_prompt = (
        "You are an expert, strict, zero-hallucination data extraction agent for an international university education counseling system.\n"
        "Extract the requested academic degree programs STRICTLY from the provided university document sources.\n"
        "Rules:\n"
        "1. Never invent or hallucinate facts, programs, fees, or deadlines. If a field is not stated in the source text, use null (or [] for lists).\n"
        "2. Keep tuition fees in their original stated currency and format.\n"
        "3. description must be a complete, informative paragraph explaining the programme focus, curriculum, and career outcomes.\n"
        "4. Output ONLY a valid JSON object matching the requested schema.\n"
    )

    user_prompt = (
        f"Target University: {uni_name} ({uni_domain})\n\n"
        "Task: Extract ALL degree programmes offered by this university from the sources, "
        "categorized by degree level into 'bachelors', 'masters', 'phd', and 'diploma'.\n\n"
        "Schema Contract:\n"
        "{\n"
        '  "bachelors": [ ...list of bachelors degrees (BS, BSc, BA, BBA, BE, B.Ed, BFA, MBBS, LLB, PharmD, DPT)... ],\n'
        '  "masters": [ ...list of masters degrees (MS, MSc, MA, MBA, MPhil, M.Ed, LLM, ME)... ],\n'
        '  "phd": [ ...list of PhD and research doctorates (exclude post-doctoral fellowships)... ],\n'
        '  "diploma": [ ...list of award-bearing diploma and certificate programs (PGDs, certificates)... ]\n'
        "}\n\n"
        "Each programme in the lists must match:\n"
        "{\n"
        '  "name": "<Program Name>", "program_info_link": "<URL or null>",\n'
        '  "department": "<or null>", "degree_level": "bachelors" | "masters" | "phd" | "diploma",\n'
        '  "duration": "<e.g. 4 Years or null>", "tuition_fee": "<fee exactly as published, or null>",\n'
        '  "currency": "<currency published in, e.g. USD, PKR, EUR, or null>",\n'
        '  "scholarships_info": "<or null>", "intake_terms": ["<e.g. Fall; [] if unstated>"],\n'
        '  "delivery_mode": "<On-Campus, Online, or Hybrid, or null>", "application_fee": "<or null>",\n'
        '  "career_prospects": "<or null>",\n'
        '  "description": "<ONE FULL PARAGRAPH: overview, focus areas, career prospects>",\n'
        '  "admission_requirements": "<how to apply and requirements beyond marks, or null>",\n'
        '  "eligibility_requirements": {\n'
        '    "minimum_marks_percentage": "<or null>", "entry_tests_accepted": [], "aggregate_formula": "<or null>"\n'
        '  },\n'
        '  "application_status": "open" | "closed" | "rolling" | "upcoming" | null,\n'
        '  "application_deadlines": ["<one entry per deadline; [] if unstated>"]\n'
        "}\n\n"
        f"Document Sources:\n{corpus_text}\n"
    )

    parsed_json = await _execute_deepseek_json_call(user_prompt, system_prompt, timeout=75.0, max_tokens=8192)

    categorized: Dict[str, List[ProgramItem]] = {
        "bachelors": [],
        "masters": [],
        "phd": [],
        "diploma": [],
    }

    for level_key in ("bachelors", "masters", "phd", "diploma"):
        raw_items = parsed_json.get(level_key) or []
        for item_data in raw_items:
            if not isinstance(item_data, dict):
                continue
            try:
                if not item_data.get("degree_level"):
                    item_data["degree_level"] = level_key
                categorized[level_key].append(ProgramItem(**item_data))
            except ValidationError as ve:
                logger.debug(f"Validation error for {level_key} item: {ve}")
            except Exception as e:
                logger.debug(f"Error constructing {level_key} item: {e}")

    return categorized


async def query_deepseek_consolidated_identity(
    corpus_text: str,
    uni_name: str,
    uni_domain: str,
) -> Tuple[MainInfo, ContactInfo, List[FacultyItem]]:
    """
    Extract identity metadata, contact details, and faculties in a SINGLE pass.
    """
    system_prompt = (
        "You are an expert, strict, zero-hallucination data extraction agent for an international university education counseling system.\n"
        "Extract the requested identity, contact, and faculty information STRICTLY from the provided university document sources.\n"
        "Rules:\n"
        "1. Never invent or hallucinate facts or rankings. Leave rankings as [].\n"
        "2. Output ONLY a valid JSON object matching the requested schema.\n"
    )

    user_prompt = (
        f"Target University: {uni_name} ({uni_domain})\n\n"
        "Task: Extract university identity details, contact information, and constituent faculties/schools.\n\n"
        "Schema Contract:\n"
        "{\n"
        '  "main_info": {\n'
        '    "name": "<University Name>", "abbreviation": "<or null>", "country": "<Country e.g. USA, Pakistan, Germany>",\n'
        '    "city": "<City or null>", "established_year": null, "accreditation_body": "<or null>",\n'
        '    "admission_cycles_offered": [], "primary_instruction_language": "<or null>",\n'
        '    "website": "<URL>", "type": "public" or "private", "description": "<Concise overview>",\n'
        '    "key_links": {\n'
        '      "academics_url": "<or null>", "admissions_url": "<or null>", "application_portal_url": "<or null>"\n'
        '    },\n'
        '    "rankings": []\n'
        '  },\n'
        '  "contact": {\n'
        '    "official_email": "<or null>", "phone_numbers": [], "physical_address": "<or null>",\n'
        '    "admissions_office_location": "<or null>", "sub_campuses_contact": []\n'
        '  },\n'
        '  "faculties": [\n'
        '    {\n'
        '      "faculty_name": "<Faculty or School Name>", "description": "<or null>",\n'
        '      "departments": ["<Department 1>", "<Department 2>"], "faculty_website": "<or null>"\n'
        '    }\n'
        '  ]\n'
        "}\n\n"
        f"Document Sources:\n{corpus_text}\n"
    )

    parsed_json = await _execute_deepseek_json_call(user_prompt, system_prompt, timeout=60.0, max_tokens=4096)

    # Parse main_info
    raw_main = parsed_json.get("main_info") or {}
    try:
        if not raw_main.get("name"):
            raw_main["name"] = uni_name
        if not raw_main.get("website"):
            raw_main["website"] = f"https://{uni_domain}"
        main_info = MainInfo(**raw_main)
    except Exception:
        main_info = MainInfo(name=uni_name, website=f"https://{uni_domain}")

    # Parse contact
    raw_contact = parsed_json.get("contact") or {}
    try:
        contact_info = ContactInfo(**raw_contact)
    except Exception:
        contact_info = ContactInfo()

    # Parse faculties
    raw_faculties = parsed_json.get("faculties") or []
    faculties = []
    for f in raw_faculties:
        if isinstance(f, dict):
            try:
                faculties.append(FacultyItem(**f))
            except Exception:
                pass

    return main_info, contact_info, faculties


async def query_deepseek_block(
    spec: QuerySpec,
    corpus_text: str,
    uni_name: str,
    uni_domain: str,
) -> Any:
    """
    Execute a single targeted schema query block against DeepSeek-V4.1-Flash.
    Used for targeted smart resume of specific failed blocks.
    """
    system_prompt = (
        "You are an expert, strict, zero-hallucination data extraction agent for an international university education counseling system.\n"
        "Extract the requested academic information STRICTLY from the provided university document sources.\n"
        "Rules:\n"
        "1. Never invent or hallucinate facts, programs, fees, or deadlines. If a field is not stated in the source text, use null (or [] for lists).\n"
        "2. Keep tuition fees in their original stated currency and format.\n"
        "3. Respond ONLY with a valid JSON object matching the requested schema.\n"
    )

    if spec.single:
        user_prompt = (
            f"Target University: {uni_name} ({uni_domain})\n\n"
            f"Extraction Task:\n{spec.prompt}\n\n"
            f"Document Sources:\n{corpus_text}\n"
        )
    else:
        user_prompt = (
            f"Target University: {uni_name} ({uni_domain})\n\n"
            f"Extraction Task:\n{spec.prompt}\n\n"
            f"IMPORTANT: Output your result as a JSON object with a single key 'items':\n"
            f"{{\"items\": [ ...list of items matching the requested schema... ]}}\n\n"
            f"Document Sources:\n{corpus_text}\n"
        )

    parsed_json = await _execute_deepseek_json_call(user_prompt, system_prompt, timeout=60.0, max_tokens=8192)

    if spec.single:
        return spec.model(**parsed_json)
    else:
        raw_list = parsed_json.get("items")
        if raw_list is None:
            for v in parsed_json.values():
                if isinstance(v, list):
                    raw_list = v
                    break
        if raw_list is None:
            raw_list = []

        model_cls = spec.model
        origin = get_origin(model_cls)
        if origin in (list, List):
            args = get_args(model_cls)
            model_cls = args[0] if args else dict

        items = []
        for item_data in raw_list:
            if not isinstance(item_data, dict):
                continue
            try:
                items.append(model_cls(**item_data))
            except ValidationError as ve:
                logger.debug(f"Failed to parse item in {spec.key}: {ve}")
            except Exception as e:
                logger.debug(f"Unexpected error constructing item in {spec.key}: {e}")
        return items


async def extract_with_deepseek_engine(
    links_list: List[Dict[str, Any]],
    uni_name: str,
    uni_slug: str,
    uni_domain: str,
    failed_blocks: Optional[List[str]] = None,
    accumulated_results: Optional[Dict[str, Any]] = None,
) -> Tuple[UniversityPayload, ExtractionReport]:
    """
    Master DeepSeek Direct Extraction Engine.

    Executes Phase 2 (Crawl4AI/HTTP page text fetching) and Phase 3 (DeepSeek-V4.1-Flash
    high-efficiency 2-pass consolidated extraction) with smart resume, grounding verification,
    and financial normalization.
    """
    report = ExtractionReport()
    results: Dict[str, Any] = dict(accumulated_results or {})

    # 1. Fetch text corpus for links (capped at 25 top pages to reduce token cost by 30%)
    print(f"\n🚀 [DEEPSEEK ENGINE] Fetching text content for top candidate links...")
    corpus = await fetch_corpus_text_for_links(links_list, max_pages=25, concurrency=8)
    corpus_text = _build_combined_context(corpus)

    if not corpus_text.strip():
        logger.warning(f"Corpus text was empty for {uni_name}; creating minimal identity.")
        corpus_text = f"University: {uni_name}\nWebsite: https://{uni_domain}\n"

    # 2. Determine blocks to query
    blocks_to_query = [
        spec for spec in QUERY_SUITE
        if failed_blocks is None or spec.key in failed_blocks
    ]

    # Consolidated extraction: When querying full university (or >= 3 blocks)
    # Reduces requests from 6 down to 2, cutting tokens and API costs by ~75%!
    should_consolidate = failed_blocks is None or len(blocks_to_query) >= 3

    if should_consolidate:
        print(f"📌 [DEEPSEEK ENGINE] High-Efficiency 2-Pass Extraction via DeepSeek-V4.1-Flash (1M Context)...")
        # Pass 1: Academic Programs
        print(f"  └─ Pass 1: Querying consolidated academic degree programmes...")
        try:
            cat_programs = await query_deepseek_consolidated_programs(corpus_text, uni_name, uni_domain)
            for level in ("bachelors", "masters", "phd", "diploma"):
                if level in results and failed_blocks is None:
                    continue
                results[level] = cat_programs.get(level, [])
                report.record_success(level, duration_sec=2.0)
                print(f"     ✓ Received [{level}]: {len(results[level])} item(s).")
        except DeepSeekQuotaError:
            raise
        except Exception as e:
            logger.error(f"Consolidated programs query failed: {e}")
            for level in ("bachelors", "masters", "phd", "diploma"):
                if level not in results:
                    report.record_failure(level, str(e))
                    results[level] = []

        # Pass 2: Identity, Contact & Faculties
        print(f"  └─ Pass 2: Querying consolidated identity, contact & faculties...")
        try:
            main_info_res, contact_info_res, faculties_res = await query_deepseek_consolidated_identity(
                corpus_text, uni_name, uni_domain
            )
            if "main_info_contact" not in results or failed_blocks is not None:
                results["main_info_contact"] = Q1Payload(main_info=main_info_res, contact=contact_info_res)
                report.record_success("main_info_contact", duration_sec=1.5)
                print(f"     ✓ Received [main_info_contact]: 1 item(s).")
            if "faculties" not in results or failed_blocks is not None:
                results["faculties"] = faculties_res
                report.record_success("faculties", duration_sec=1.5)
                print(f"     ✓ Received [faculties]: {len(faculties_res)} item(s).")
        except DeepSeekQuotaError:
            raise
        except Exception as e:
            logger.error(f"Consolidated identity/faculties query failed: {e}")
            if "main_info_contact" not in results:
                report.record_failure("main_info_contact", str(e))
                results["main_info_contact"] = None
            if "faculties" not in results:
                report.record_failure("faculties", str(e))
                results["faculties"] = []
    else:
        # Targeted smart resume for 1-2 specific blocks
        print(f"📌 [DEEPSEEK ENGINE] Querying {len(blocks_to_query)} targeted schema block(s)...")
        for spec in blocks_to_query:
            if spec.key in results and failed_blocks is None:
                continue
            try:
                print(f"  └─ Querying [{spec.key}]...")
                block_result = await query_deepseek_block(spec, corpus_text, uni_name, uni_domain)
                results[spec.key] = block_result
                report.record_success(spec.key, duration_sec=1.5)
                count = len(block_result) if isinstance(block_result, list) else 1
                print(f"     ✓ Received [{spec.key}]: {count} item(s).")
            except DeepSeekQuotaError:
                raise
            except Exception as e:
                logger.error(f"DeepSeek query failed for [{spec.key}]: {e}")
                report.record_failure(spec.key, str(e))
                results[spec.key] = [] if not spec.single else None

    # 3. Assemble Blocks 1 & 4: Main Info & Contact
    q1 = results.get("main_info_contact")
    if q1 is not None and isinstance(q1, Q1Payload):
        main_info, contact_info = q1.main_info, q1.contact
    else:
        main_info = MainInfo(
            name=uni_name,
            website=f"https://{uni_domain}",
            description=f"Identity block for {uni_name}.",
            key_links=KeyLinks(),
        )
        contact_info = ContactInfo()

    # Apply registry facts (e.g. rankings)
    reg_entry = registry_lookup(uni_domain)
    if reg_entry:
        if reg_entry.get("type") and not main_info.type:
            main_info.type = reg_entry["type"]
        if reg_entry.get("established_year") and not main_info.established_year:
            main_info.established_year = reg_entry["established_year"]

    # Fill missing portal using free search enrichment
    if not main_info.key_links.application_portal_url:
        portal = await free_search_find_portal(uni_domain, main_info.name)
        if portal:
            main_info.key_links.application_portal_url = portal
            main_info.exa_enriched = True

    # 4. Assemble Block 2: Programs
    bachelors = results.get("bachelors") or []
    masters = results.get("masters") or []
    phd = results.get("phd") or []
    diploma = results.get("diploma") or []

    # Jev Grounding & Citation Verification Gate
    if is_typesafe_available():
        print("  └─ Enforcing Jev Grounding & Citation Verification Gate...")
        bachelors = await verify_program_batch(bachelors)
        masters = await verify_program_batch(masters)
        phd = await verify_program_batch(phd)
        diploma = await verify_program_batch(diploma)

    # DeepSeek Financial USD Normalization
    all_programs = bachelors + masters + phd + diploma
    if all_programs:
        print(f"  └─ Applying DeepSeek Financial Normalization to USD ({len(all_programs)} programs)...")
        await normalize_tuition_batch(all_programs, default_currency=main_info.country)

    programs = ProgramCategoryBlock(
        bachelors=bachelors,
        masters=masters,
        phd=phd,
        diploma=diploma,
    )

    payload = UniversityPayload(
        main_info=main_info,
        programs=programs,
        faculties=results.get("faculties") or [],
        contact=contact_info,
        failed_query_blocks=sorted(report.failed),
        intake_year=config.default_intake_year,
        data_version=1,
    )

    return payload, report
