"""
Gemini Direct Extraction Engine (gemini_extractor.py).

Same architecture as the DeepSeek engine -- scrape the harvested links, build one
text corpus, extract it in two consolidated passes, then post-process -- with the
official Google Gemini API in place of DeepSeek, and native Pydantic schema
enforcement in place of a JSON-mode contract.

Page fetching is imported from deepseek_extractor rather than duplicated: those
functions are plain httpx and have no engine-specific logic in them.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, get_args, get_origin

from pydantic import BaseModel, ValidationError

from src.config import config
from src.extractor.crawlers.deepseek_extractor import (
    _build_combined_context,
    fetch_corpus_text_for_links,
)
from src.extractor.crawlers.free_search_enrichment import free_search_find_portal
from src.extractor.crawlers.query_schemas import (
    CONSOLIDATED_IDENTITY_SYSTEM_PROMPT,
    CONSOLIDATED_PROGRAMS_SYSTEM_PROMPT,
    PROGRAM_LEVELS,
    QUERY_SUITE,
    TARGETED_BLOCK_SYSTEM_PROMPT,
    ExtractionReport,
    IdentityResponse,
    ProgramsResponse,
    Q1Payload,
    QuerySpec,
    build_consolidated_identity_prompt,
    build_consolidated_programs_prompt,
    build_targeted_block_prompt,
)
from src.extractor.crawlers.verification import verify_program_batch
from src.utilities.deepseek_client import normalize_tuition_batch
from src.utilities.gemini_client import GeminiQuotaError, gemini_generate_json
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


def _loads_lenient(raw: str) -> Dict[str, Any]:
    """Parse model output that was returned as text rather than a parsed object."""
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    if not cleaned:
        return {}
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        from src.extractor.crawlers.json_repairing import (
            extract_json_str,
            sanitize_invalid_escapes,
        )
        candidate = sanitize_invalid_escapes(extract_json_str(cleaned))
        candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            logger.warning("Gemini returned text that could not be repaired into JSON.")
            return {}


def _as_dict(result: Any) -> Dict[str, Any]:
    """Normalise whatever gemini_generate_json returned into a plain dict."""
    if result is None:
        return {}
    if isinstance(result, BaseModel):
        return result.model_dump()
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        return _loads_lenient(result)
    return {}


def _build_items(raw_items: Any, model_cls: Any, label: str) -> List[Any]:
    """Construct a list of schema items, dropping only the entries that fail."""
    items: List[Any] = []
    for item_data in raw_items or []:
        if isinstance(item_data, BaseModel):
            items.append(item_data)
            continue
        if not isinstance(item_data, dict):
            continue
        try:
            items.append(model_cls(**item_data))
        except ValidationError as ve:
            logger.debug(f"Validation error for {label} item: {ve}")
        except Exception as e:  # noqa: BLE001 - one bad item must not sink the block
            logger.debug(f"Error constructing {label} item: {e}")
    return items


async def query_gemini_consolidated_programs(
    corpus_text: str,
    uni_name: str,
    uni_domain: str,
) -> Dict[str, List[ProgramItem]]:
    """
    Pass 1: every degree programme, all four levels, in a single request.
    """
    result = await gemini_generate_json(
        user_prompt=build_consolidated_programs_prompt(uni_name, uni_domain, corpus_text),
        system_instruction=CONSOLIDATED_PROGRAMS_SYSTEM_PROMPT,
        response_schema=ProgramsResponse,
        timeout=120.0,
    )
    parsed = _as_dict(result)

    categorized: Dict[str, List[ProgramItem]] = {level: [] for level in PROGRAM_LEVELS}
    for level in PROGRAM_LEVELS:
        raw_items = parsed.get(level) or []
        for item_data in raw_items:
            if isinstance(item_data, dict) and not item_data.get("degree_level"):
                item_data["degree_level"] = level
        categorized[level] = _build_items(raw_items, ProgramItem, level)
    return categorized


async def query_gemini_consolidated_identity(
    corpus_text: str,
    uni_name: str,
    uni_domain: str,
) -> Tuple[MainInfo, ContactInfo, List[FacultyItem]]:
    """
    Pass 2: identity metadata, contact details and faculties in a single request.
    """
    result = await gemini_generate_json(
        user_prompt=build_consolidated_identity_prompt(uni_name, uni_domain, corpus_text),
        system_instruction=CONSOLIDATED_IDENTITY_SYSTEM_PROMPT,
        response_schema=IdentityResponse,
        timeout=90.0,
    )
    parsed = _as_dict(result)

    raw_main = parsed.get("main_info") or {}
    try:
        if not raw_main.get("name"):
            raw_main["name"] = uni_name
        if not raw_main.get("website"):
            raw_main["website"] = f"https://{uni_domain}"
        main_info = MainInfo(**raw_main)
    except Exception:  # noqa: BLE001 - identity must never fail the university
        main_info = MainInfo(
            name=uni_name,
            website=f"https://{uni_domain}",
            description=f"Identity block for {uni_name}.",
            key_links=KeyLinks(),
        )

    try:
        contact_info = ContactInfo(**(parsed.get("contact") or {}))
    except Exception:  # noqa: BLE001
        contact_info = ContactInfo()

    faculties = _build_items(parsed.get("faculties"), FacultyItem, "faculties")
    return main_info, contact_info, faculties


async def query_gemini_block(
    spec: QuerySpec,
    corpus_text: str,
    uni_name: str,
    uni_domain: str,
) -> Any:
    """
    Execute one targeted schema block, for smart resume of specific failures.
    """
    model_cls = spec.model
    if not spec.single:
        origin = get_origin(model_cls)
        if origin in (list, List):
            args = get_args(model_cls)
            model_cls = args[0] if args else dict

    response_schema = spec.model if spec.single else List[model_cls]

    result = await gemini_generate_json(
        user_prompt=build_targeted_block_prompt(spec, uni_name, uni_domain, corpus_text),
        system_instruction=TARGETED_BLOCK_SYSTEM_PROMPT,
        response_schema=response_schema,
        timeout=90.0,
    )

    if spec.single:
        if isinstance(result, spec.model):
            return result
        return spec.model(**_as_dict(result))

    if isinstance(result, list):
        return _build_items(result, model_cls, spec.key)

    parsed = _as_dict(result)
    raw_list = parsed.get("items")
    if raw_list is None:
        for value in parsed.values():
            if isinstance(value, list):
                raw_list = value
                break
    return _build_items(raw_list, model_cls, spec.key)


async def extract_with_gemini_engine(
    links_list: List[Dict[str, Any]],
    uni_name: str,
    uni_slug: str,
    uni_domain: str,
    failed_blocks: Optional[List[str]] = None,
    accumulated_results: Optional[Dict[str, Any]] = None,
) -> Tuple[UniversityPayload, ExtractionReport]:
    """
    Master Gemini Direct Extraction Engine.

    Fetches page text for the harvested links, runs the two consolidated
    extraction passes (or targeted blocks during a smart resume), then applies
    the shared post-processing: registry facts, portal search, Jev grounding and
    financial normalization.
    """
    report = ExtractionReport()
    results: Dict[str, Any] = dict(accumulated_results or {})

    # 1. Fetch the text corpus for the selected links.
    print(f"\n🚀 [GEMINI ENGINE] Fetching text content for top candidate links...")
    corpus = await fetch_corpus_text_for_links(links_list, max_pages=25, concurrency=8)
    corpus_text = _build_combined_context(corpus)

    if not corpus_text.strip():
        logger.warning(f"Corpus text was empty for {uni_name}; creating minimal identity.")
        corpus_text = f"University: {uni_name}\nWebsite: https://{uni_domain}\n"

    # 2. Decide which blocks this run owes.
    blocks_to_query = [
        spec for spec in QUERY_SUITE
        if failed_blocks is None or spec.key in failed_blocks
    ]
    should_consolidate = failed_blocks is None or len(blocks_to_query) >= 3

    if should_consolidate:
        model_name = getattr(config, "gemini_model", "") or "gemini-2.0-flash"
        print(f"📌 [GEMINI ENGINE] High-Efficiency 2-Pass Extraction via {model_name}...")

        # Pass 1: academic programmes.
        print(f"  └─ Pass 1: Querying consolidated academic degree programmes...")
        try:
            cat_programs = await query_gemini_consolidated_programs(corpus_text, uni_name, uni_domain)
            for level in PROGRAM_LEVELS:
                if level in results and failed_blocks is None:
                    continue
                results[level] = cat_programs.get(level, [])
                report.record_success(level, duration_sec=2.0)
                print(f"     ✓ Received [{level}]: {len(results[level])} item(s).")
        except GeminiQuotaError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Consolidated programs query failed: {e}")
            for level in PROGRAM_LEVELS:
                if level not in results:
                    report.record_failure(level, str(e))
                    results[level] = []

        # Pass 2: identity, contact and faculties.
        print(f"  └─ Pass 2: Querying consolidated identity, contact & faculties...")
        try:
            main_info_res, contact_info_res, faculties_res = await query_gemini_consolidated_identity(
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
        except GeminiQuotaError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Consolidated identity/faculties query failed: {e}")
            if "main_info_contact" not in results:
                report.record_failure("main_info_contact", str(e))
                results["main_info_contact"] = None
            if "faculties" not in results:
                report.record_failure("faculties", str(e))
                results["faculties"] = []
    else:
        # Targeted smart resume for one or two specific blocks.
        print(f"📌 [GEMINI ENGINE] Querying {len(blocks_to_query)} targeted schema block(s)...")
        for spec in blocks_to_query:
            if spec.key in results and failed_blocks is None:
                continue
            try:
                print(f"  └─ Querying [{spec.key}]...")
                block_result = await query_gemini_block(spec, corpus_text, uni_name, uni_domain)
                results[spec.key] = block_result
                report.record_success(spec.key, duration_sec=1.5)
                count = len(block_result) if isinstance(block_result, list) else 1
                print(f"     ✓ Received [{spec.key}]: {count} item(s).")
            except GeminiQuotaError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error(f"Gemini query failed for [{spec.key}]: {e}")
                report.record_failure(spec.key, str(e))
                results[spec.key] = [] if not spec.single else None

    # 3. Blocks 1 & 4: identity and contact.
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

    # Sourced identity facts come from the registry, never from the model.
    reg_entry = registry_lookup(uni_domain)
    if reg_entry:
        if reg_entry.get("type") and not main_info.type:
            main_info.type = reg_entry["type"]
        if reg_entry.get("established_year") and not main_info.established_year:
            main_info.established_year = reg_entry["established_year"]

    if not main_info.key_links.application_portal_url:
        portal = await free_search_find_portal(uni_domain, main_info.name)
        if portal:
            main_info.key_links.application_portal_url = portal
            main_info.exa_enriched = True

    # 4. Block 2: programmes.
    bachelors = results.get("bachelors") or []
    masters = results.get("masters") or []
    phd = results.get("phd") or []
    diploma = results.get("diploma") or []

    if is_typesafe_available():
        print("  └─ Enforcing Jev Grounding & Citation Verification Gate...")
        bachelors = await verify_program_batch(bachelors)
        masters = await verify_program_batch(masters)
        phd = await verify_program_batch(phd)
        diploma = await verify_program_batch(diploma)

    all_programs = bachelors + masters + phd + diploma
    if all_programs:
        print(f"  └─ Applying Financial Normalization to USD ({len(all_programs)} programs)...")
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
