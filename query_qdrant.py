#!/usr/bin/env python3
"""
Standalone Qdrant Vector Database Query Script for AI Education Counselor
-------------------------------------------------------------------------
Loads credentials from .env and executes semantic vector search against
your live Qdrant Cloud collection ('education_counselor').

Usage:
  1. Interactive Mode:
     python3 query_qdrant.py

  2. Command Line Mode:
     python3 query_qdrant.py --query "BS Computer Science tuition fee" --top-k 5
"""
import os
import sys
import argparse
from pathlib import Path

# Ensure root directory is in sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Minimal .env loader
def _load_dotenv(path: Path = BASE_DIR / ".env") -> None:
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass

_load_dotenv()

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.prompt import Prompt
    console = Console()
except ImportError:
    class FallbackConsole:
        def print(self, text, *args, **kwargs):
            print(text)
    console = FallbackConsole()

try:
    from qdrant_client import QdrantClient
    from sentence_transformers import SentenceTransformer
except ImportError as e:
    console.print(f"[bold red]Missing required package:[/bold red] {e}")
    console.print("Please install requirements: [bold magenta]pip install qdrant-client sentence-transformers[/bold magenta]")
    sys.exit(1)


def query_qdrant(
    query_text: str,
    top_k: int = 5,
    collection_name: str = "education_counselor",
    qdrant_url: str = None,
    qdrant_api_key: str = None,
    model_name: str = "BAAI/bge-base-en-v1.5",
):
    """Encodes query_text and retrieves top_k nearest vector points from Qdrant Cloud."""
    url = qdrant_url or os.getenv("QDRANT_URL", "http://localhost:6333")
    api_key = qdrant_api_key or os.getenv("QDRANT_API_KEY", "")
    coll = os.getenv("QDRANT_COLLECTION_NAME", collection_name)

    # Add instruction prefix for BAAI/bge models if not already present
    if "bge-" in model_name.lower() and not query_text.startswith("Represent this sentence"):
        formatted_query = f"Represent this sentence for searching relevant passages: {query_text}"
    else:
        formatted_query = query_text

    console.print(f"[bold cyan]🔍 Encoding query using model '[yellow]{model_name}[/yellow]'...[/bold cyan]")
    model = SentenceTransformer(model_name)
    query_vector = model.encode(formatted_query, normalize_embeddings=True).tolist()

    console.print(f"[bold cyan]⚡ Connecting to Qdrant Cloud ([yellow]{url}[/yellow])...[/bold cyan]")
    client = QdrantClient(url=url, api_key=api_key if api_key else None)

    try:
        search_results = client.query_points(
            collection_name=coll,
            query=query_vector,
            limit=top_k,
        )
    except Exception as e:
        console.print(f"[bold red]Qdrant Query Error:[/bold red] {e}")
        return []

    points = search_results.points
    if not points:
        console.print(f"[bold yellow]No matching records found in collection '{coll}'.[/bold yellow]")
        return []

    # Format Results Table
    table = Table(title=f"🎓 Counselor Vector DB Search Results for: '{query_text}'", show_header=True, header_style="bold magenta")
    table.add_column("#", style="dim", width=3)
    table.add_column("Score", style="bold green", width=8)
    table.add_column("University", style="cyan", width=25)
    table.add_column("Program Name", style="bold yellow", width=25)
    table.add_column("Fee / Portal", style="white", width=30)
    table.add_column("Summary / Excerpt", style="dim", width=40)

    for idx, point in enumerate(points, 1):
        payload = point.payload or {}
        score_pct = f"{point.score * 100:.1f}%"
        uni = payload.get("uni_name", "N/A")
        prog = payload.get("program_name") or payload.get("block_type", "General Info")
        fee = payload.get("tuition_fee") or "N/A"
        portal = payload.get("portal_url") or payload.get("main_website") or ""
        summary = payload.get("summary") or payload.get("text_chunk") or ""
        
        fee_portal_str = f"Fee: {fee}\nPortal: {portal}" if portal else f"Fee: {fee}"
        short_summary = summary[:120] + "..." if len(summary) > 120 else summary

        table.add_row(
            str(idx),
            score_pct,
            uni,
            prog,
            fee_portal_str,
            short_summary,
        )

    console.print(table)

    # Detailed Cards Output
    console.print("\n[bold green]📌 Detailed Result Cards:[/bold green]")
    for idx, point in enumerate(points, 1):
        payload = point.payload or {}
        score = point.score
        uni = payload.get("uni_name", "N/A")
        prog = payload.get("program_name") or payload.get("block_type", "Info Block")
        dept = payload.get("department", "N/A")
        elig = payload.get("eligibility", "N/A")
        fee = payload.get("tuition_fee", "N/A")
        portal = payload.get("portal_url") or payload.get("main_website", "N/A")
        summary = payload.get("summary") or payload.get("text_chunk", "")

        card_text = (
            f"[bold cyan]University:[/bold cyan] {uni}\n"
            f"[bold yellow]Program:[/bold yellow] {prog}\n"
            f"[bold white]Department:[/bold white] {dept}\n"
            f"[bold white]Eligibility:[/bold white] {elig}\n"
            f"[bold green]Tuition Fee:[/bold green] {fee}\n"
            f"[bold blue]Portal Link:[/bold blue] [link={portal}]{portal}[/link]\n"
            f"[bold dim]Summary:[/bold dim] {summary}"
        )
        console.print(Panel(card_text, title=f"Result #{idx} | Similarity Match: {score*100:.1f}%", border_style="cyan"))

    return points


def main():
    parser = argparse.ArgumentParser(description="Standalone Qdrant Vector Database Search Script")
    parser.add_argument("--query", "-q", type=str, help="Search query string")
    parser.add_argument("--top-k", "-k", type=int, default=5, help="Number of results to retrieve (default: 5)")
    parser.add_argument("--collection", "-c", type=str, default="education_counselor", help="Qdrant collection name")
    args = parser.parse_args()

    if args.query:
        query_qdrant(query_text=args.query, top_k=args.top_k, collection_name=args.collection)
    else:
        console.print(Panel.fit("[bold cyan]🎓 Education Counselor Qdrant Cloud Search REPL[/bold cyan]\nType your search query or '[bold red]exit[/bold red]' to quit."))
        while True:
            try:
                user_q = Prompt.ask("\n[bold yellow]Enter Student Query[/bold yellow]")
                if not user_q or user_q.strip().lower() in ("exit", "quit", "q"):
                    console.print("[dim]Goodbye![/dim]")
                    break
                query_qdrant(query_text=user_q, top_k=args.top_k, collection_name=args.collection)
            except KeyboardInterrupt:
                console.print("\n[dim]Exiting...[/dim]")
                break


if __name__ == "__main__":
    main()
