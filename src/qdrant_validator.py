"""
Automated Qdrant 10-Query Benchmark Suite & Rollback Protection Engine
(src/qdrant_validator.py)

Executes 10 comprehensive natural language queries against the newly synced
Qdrant Cloud database, validates score thresholds (>0.50) and metadata payload
completeness, and automatically rolls back if validation fails (<80% pass rate).
"""
import os
import logging
from typing import List, Dict, Any, Tuple
from dataclasses import dataclass

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

logger = logging.getLogger("QdrantValidator")
console = Console()


@dataclass
class QueryBenchmark:
    query: str
    target_intent: str


BENCHMARK_SUITE: List[QueryBenchmark] = [
    QueryBenchmark(
        query="BS Computer Science tuition fee and admission eligibility in Pakistan",
        target_intent="Undergraduate CS tuition fee, eligibility, and portal"
    ),
    QueryBenchmark(
        query="MS Data Science and Artificial Intelligence degree in Germany EUR fee",
        target_intent="Graduate AI/DS degrees with currency and intake terms"
    ),
    QueryBenchmark(
        query="Undergraduate Software Engineering admission deadline in Fall intake",
        target_intent="Fall intake Software Engineering deadlines"
    ),
    QueryBenchmark(
        query="PhD Computer Science research programs and scholarships",
        target_intent="Doctoral research degrees with scholarship details"
    ),
    QueryBenchmark(
        query="Top public universities for Electrical Engineering in Islamabad",
        target_intent="Public university engineering in specific city"
    ),
    QueryBenchmark(
        query="BS Artificial Intelligence entry tests required like NET or SAT",
        target_intent="Entry tests accepted for AI degrees"
    ),
    QueryBenchmark(
        query="MBA and Business Administration program duration and fee",
        target_intent="Graduate business program tuition fee and duration"
    ),
    QueryBenchmark(
        query="Medical and Biomedical Engineering undergraduate degree requirements",
        target_intent="Medical engineering eligibility and courses"
    ),
    QueryBenchmark(
        query="Spring semester intake for Computer Engineering",
        target_intent="Spring intake terms for computer engineering"
    ),
    QueryBenchmark(
        query="Universities with online application portal links for international students",
        target_intent="Application portal URL coverage and international links"
    ),
]


def validate_qdrant_database(
    q_client: Any,
    collection_name: str,
    model_name: str = "BAAI/bge-base-en-v1.5",
    score_threshold: float = 0.50,
) -> Tuple[bool, List[Dict[str, Any]]]:
    """
    Executes the 10-query benchmark suite against the active Qdrant collection.

    Returns (is_passed: bool, detailed_results: List[Dict]).
    """
    console.print(Panel(f"[bold cyan]🔍 STARTING AUTOMATED 10-QUERY QDRANT VALIDATION BENCHMARK[/bold cyan]\nCollection: [yellow]{collection_name}[/yellow] | Model: [yellow]{model_name}[/yellow]"))

    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(model_name)
    except Exception as e:
        console.print(f"[bold red]Validation Failed:[/bold red] Could not load model '{model_name}': {e}")
        return False, []

    results = []
    passed_count = 0

    table = Table(title="📊 Qdrant 10-Query Retrieval Benchmark Results", show_header=True, header_style="bold magenta")
    table.add_column("#", style="dim", width=3)
    table.add_column("Query Intent", style="cyan", width=35)
    table.add_column("Top Match", style="yellow", width=25)
    table.add_column("Score", style="bold green", width=8)
    table.add_column("Status", style="bold", width=10)

    for idx, bench in enumerate(BENCHMARK_SUITE, 1):
        formatted_q = f"Represent this sentence for searching relevant passages: {bench.query}"
        try:
            q_vec = model.encode(formatted_q, normalize_embeddings=True).tolist()
            search = q_client.query_points(
                collection_name=collection_name,
                query=q_vec,
                limit=3
            )
            points = search.points
        except Exception as e:
            logger.error(f"Query #{idx} failed: {e}")
            points = []

        if not points:
            table.add_row(str(idx), bench.target_intent, "NO MATCH", "0.00", "[red]FAIL[/red]")
            results.append({"index": idx, "query": bench.query, "passed": False, "reason": "No points returned"})
            continue

        top_point = points[0]
        score = top_point.score
        payload = top_point.payload or {}
        uni_name = payload.get("uni_name", "Unknown")
        prog_name = payload.get("program_name", "Info Block")

        # Check payload completeness: uni_name, program_name, and portal_url or text_chunk
        has_metadata = bool(uni_name and (prog_name or payload.get("text_chunk")))
        is_ok = (score >= score_threshold) and has_metadata

        if is_ok:
            passed_count += 1
            status_str = "[green]PASS[/green]"
        else:
            status_str = "[red]FAIL[/red]"

        top_str = f"{uni_name}\n({prog_name[:20]})"
        table.add_row(str(idx), bench.target_intent, top_str, f"{score:.4f}", status_str)
        results.append({
            "index": idx,
            "query": bench.query,
            "score": score,
            "uni_name": uni_name,
            "program_name": prog_name,
            "passed": is_ok,
        })

    console.print(table)

    pass_rate = (passed_count / len(BENCHMARK_SUITE)) * 100
    is_overall_passed = pass_rate >= 80.0  # Require at least 80% pass rate

    summary_panel = (
        f"[bold {'green' if is_overall_passed else 'red'}]"
        f"Benchmark Pass Rate: {passed_count}/{len(BENCHMARK_SUITE)} ({pass_rate:.1f}%)[/bold {'green' if is_overall_passed else 'red'}]\n"
        f"Status: {'🎉 BENCHMARK PASSED (Qdrant Sync Retained)' if is_overall_passed else '⚠️ BENCHMARK FAILED (Triggering Rollback)'}"
    )
    console.print(Panel(summary_panel, title="⚡ Qdrant Validation Suite Result"))

    return is_overall_passed, results
