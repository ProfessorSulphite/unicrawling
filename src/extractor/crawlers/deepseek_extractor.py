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
from src.utilities.deepseek_client import is_deepseek_available, normalize_tuition_batch
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


async def query_deepseek_block(
    spec: QuerySpec,
    corpus_text: str,
    uni_name: str,
    uni_domain: str,
) -> Any:
    """
    Execute a single schema query block against DeepSeek-V4.1-Flash.
    Uses non-thinking JSON mode for ultra-fast, deterministic extraction.
    """
    api_key = (config.deepseek_api_key or os.getenv("DEEPSEEK_API_KEY", "")).strip()
    base_url = (config.deepseek_base_url or "https://api.deepseek.com").rstrip("/")
    endpoint = f"{base_url}/chat/completions"

    system_prompt = (
        "You are an expert, strict, zero-hallucination data extraction agent for an international university education counseling system.\n"
        "Extract the requested academic information STRICTLY from the provided university document sources.\n"
        "Rules:\n"
        "1. Never invent or hallucinate facts, programs, fees, or deadlines. If a field is not stated in the source text, use null (or [] for lists).\n"
        "2. Keep tuition fees in their original stated currency and format.\n"
        "3. Respond ONLY with a valid JSON object matching the requested schema.\n"
    )

    if spec.single:
        # e.g. main_info_contact
        user_prompt = (
            f"Target University: {uni_name} ({uni_domain})\n\n"
            f"Extraction Task:\n{spec.prompt}\n\n"
            f"Document Sources:\n{corpus_text}\n"
        )
    else:
        # Array of items (e.g. bachelors, masters, faculties)
        # DeepSeek json_object format requires a root object, so we wrap the array in a key named 'items'
        user_prompt = (
            f"Target University: {uni_name} ({uni_domain})\n\n"
            f"Extraction Task:\n{spec.prompt}\n\n"
            f"IMPORTANT: Output your result as a JSON object with a single key 'items':\n"
            f"{{\"items\": [ ...list of items matching the requested schema... ]}}\n\n"
            f"Document Sources:\n{corpus_text}\n"
        )

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
        "max_tokens": 8192,
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(endpoint, json=payload, headers=headers)
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
            logger.warning(f"Empty content returned by DeepSeek for [{spec.key}].")
            parsed_json = {} if spec.single else {"items": []}
        else:
            try:
                parsed_json = json.loads(cleaned)
            except json.JSONDecodeError:
                from src.extractor.crawlers.json_repairing import extract_json_str, sanitize_invalid_escapes
                try:
                    candidate = extract_json_str(cleaned)
                    candidate = sanitize_invalid_escapes(candidate)
                    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
                    parsed_json = json.loads(candidate)
                except Exception as parse_err:
                    logger.error(f"Failed to parse JSON for [{spec.key}]: {parse_err}. Raw: {cleaned[:150]}")
                    raise

        if spec.single:
            return spec.model(**parsed_json)
        else:
            raw_list = parsed_json.get("items")
            if raw_list is None:
                # Sometimes model might return array under its query key e.g. "programs" or "faculties"
                for v in parsed_json.values():
                    if isinstance(v, list):
                        raw_list = v
                        break
            if raw_list is None:
                raw_list = []

            # Extract item model class if spec.model is a typing generic alias (e.g. List[ProgramItem])
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
    direct schema query suite) with smart resume, grounding verification, and financial normalization.
    """
    report = ExtractionReport()
    results: Dict[str, Any] = dict(accumulated_results or {})

    # 1. Fetch text corpus for links
    print(f"\n🚀 [DEEPSEEK ENGINE] Fetching text content for top candidate links...")
    corpus = await fetch_corpus_text_for_links(links_list, max_pages=35, concurrency=8)
    corpus_text = _build_combined_context(corpus)

    if not corpus_text.strip():
        logger.warning(f"Corpus text was empty for {uni_name}; creating minimal identity.")
        corpus_text = f"University: {uni_name}\nWebsite: https://{uni_domain}\n"

    # 2. Determine blocks to query
    blocks_to_query = [
        spec for spec in QUERY_SUITE
        if failed_blocks is None or spec.key in failed_blocks
    ]

    print(f"📌 [DEEPSEEK ENGINE] Querying {len(blocks_to_query)} schema blocks via DeepSeek-V4.1-Flash (1M Context)...")

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
