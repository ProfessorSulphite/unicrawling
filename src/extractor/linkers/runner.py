"""
Phase 1 entry points: HEC university discovery, the end-to-end link pipeline, the
output exporters, and the CLI.

Top of this package's dependency order -- it imports from every sibling and
nothing here is imported back.
"""

import argparse
import asyncio
import json
import os
import requests
import sys

from bs4 import BeautifulSoup
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from src.config import config

from src.extractor.linkers.constants import SLUGIFY_REGEX, logger
from src.extractor.linkers.crawling import CrawlFailure, close_shared_crawler, crawl_site_links
from src.extractor.linkers.filteration import preprocess_and_filter_links
from src.extractor.linkers.semantic_scoring import (
    allocate_proportional_tier_quotas,
    classify_and_score_links,
)


def slugify_university(name: str, url: str = "") -> str:
    """Stable filesystem-safe identifier used for per-university partitioned output."""
    host = urlparse(url).netloc.lower().replace("www.", "") if url else ""
    base = host.split(".")[0] if host else name
    slug = SLUGIFY_REGEX.sub("-", base.lower()).strip("-")
    return slug or "university"

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
                            # Not a city. The HEC directory does not publish
                            # one, and "Pakistan" in a city field is a wrong
                            # answer dressed as a right one (C19).
                            "city": None
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

async def run_pipeline(
    url: str = None,
    hec_mode: bool = False,
    hec_limit: int = 5,
    max_links: int = 100,
    threshold: Optional[float] = None,
    max_pages: Optional[int] = None,
    uptodate: bool = True,
    exclude_keywords: str = "news|events",
    output_links: str = "extracted_links.txt",
    output_detailed: str = "extracted_links_detailed.txt"
):
    """
    Main orchestration function managing single site or HEC batch processing.

    `threshold` and `max_pages` fall back to config.semantic_threshold and
    config.max_crawl_pages. They were previously plain literal defaults, and the
    orchestrator called this function without passing either -- so the two
    calibrated Config fields were never read by anything and every batch ran at
    the argparse default of 0.45. A live ITU crawl logged "78 links passed
    quality threshold (0.45); 0 retained as tier reserve": every link cleared it,
    the threshold filtered nothing, and tier quotas were doing all the selection.
    """
    threshold = config.semantic_threshold if threshold is None else threshold
    max_pages = config.max_crawl_pages if max_pages is None else max_pages

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

            # Dynamic link selection, ratio from config rather than a literal:
            # config.dynamic_link_ratio documented this knob while the hardcoded
            # 0.45 below ignored it, so changing the documented field did nothing.
            candidate_count = len(scored_links)
            ratio = config.dynamic_link_ratio
            dynamic_target = min(max(15, int(candidate_count * ratio)), max_links)
            logger.info(
                f"Dynamic Link Allocation: Discovered {candidate_count} scored links -> "
                f"selecting {dynamic_target} links ({ratio:.0%} ratio, cap={max_links})."
            )

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

    # Raised, never sys.exit()ed. SystemExit inherits from BaseException, so the
    # batch driver's `except Exception` in _drain_queue does not catch it: one
    # university whose crawl produced nothing terminated the whole process and
    # every university queued behind it never ran. CrawlFailure is the same
    # signal the per-target loop above already raises and callers already expect.
    if failed and not succeeded:
        raise CrawlFailure(
            f"all {len(failed)} target(s) produced zero links: "
            + "; ".join(f"{name}: {err}" for name, err in failed)
        )

    return {"succeeded": succeeded, "failed": failed, "links": all_processed_results}

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
        default=None,
        help=(
            "Semantic similarity score threshold between 0.0 and 1.0 "
            f"(default: config.semantic_threshold, currently {config.semantic_threshold})."
        )
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help=(
            "Maximum number of sub-pages to crawl per site "
            f"(default: config.max_crawl_pages, currently {config.max_crawl_pages})."
        )
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
        except CrawlFailure as e:
            # The CLI keeps the exit code the shell contract documents; only the
            # in-process callers are spared a SystemExit they cannot catch.
            print(f"\nFAILED: {e}", file=sys.stderr)
            raise SystemExit(1)
        finally:
            await close_shared_crawler()

    asyncio.run(_main())

if __name__ == "__main__":
    main()
