"""
The read layer: every inspector command reaches the corpus through here.

`iter_all_records` runs each payload through the normalizer on the way out, so
retired bucket names, retired degree-level values and the pre-C18 singular
deadline key are migrated on read. That is why a schema change has to keep a
migration path: this is the door every stored file comes back through.

`find_university_record` does NOT do that on its filename fast path -- it returns
the file verbatim -- so a stored record in an older shape reads differently
depending on which function found it. Carried over as-is by the C20 move; fixed
next.

Reads only. Nothing in this module writes, exports, or triggers a run.
"""
import json
from typing import Any, Dict, Iterator, List, Optional

from src.config import config


def iter_all_records() -> Iterator[Dict[str, Any]]:
    """
    Yield normalized university payloads one at a time.

    Streaming generator: only the current record plus the set of seen names is
    resident, so dataset-wide analytics no longer scale their peak RAM with the
    size of the corpus. `load_all_records()` remains the eager list form for
    callers that genuinely need random access.
    """
    try:
        from src.universal_normalizer import normalize_universal_payload
    except ImportError:
        from universal_normalizer import normalize_universal_payload

    seen_slugs = set()

    # 1. Stream the master JSONL ledger if it exists
    if config.output_jsonl_path.exists():
        with open(config.output_jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                name = data.get("main_info", {}).get("name", "")
                if name and name not in seen_slugs:
                    seen_slugs.add(name)
                    yield normalize_universal_payload(data)

    # 2. Check per-slug JSON files in uni_outputs directory
    if config.outputs_uni_outputs_dir.exists():
        for f in config.outputs_uni_outputs_dir.glob("*.json"):
            try:
                with open(f, "r", encoding="utf-8") as file_obj:
                    data = json.load(file_obj)
            except Exception:
                continue
            name = data.get("main_info", {}).get("name", "")
            if name and name not in seen_slugs:
                seen_slugs.add(name)
                yield normalize_universal_payload(data)


def load_all_records() -> List[Dict[str, Any]]:
    """Loads all university payload records from output JSONL or JSON files."""
    return list(iter_all_records())


def find_university_record(query: str) -> Optional[Dict[str, Any]]:
    """Finds a single university payload by slug, name, or abbreviation."""
    query_clean = query.lower().strip()

    # Check uni_outputs first
    if config.outputs_uni_outputs_dir.exists():
        for f in config.outputs_uni_outputs_dir.glob("*.json"):
            if query_clean in f.stem.lower():
                try:
                    with open(f, "r", encoding="utf-8") as file_obj:
                        return json.load(file_obj)
                except Exception:
                    pass

    # Fallback to searching all records
    for r in load_all_records():
        main = r.get("main_info", {})
        name = main.get("name", "").lower()
        abbr = main.get("abbreviation", "").lower()
        if (
            query_clean in name
            or query_clean in abbr
            or query_clean == name.replace(" ", "_")
        ):
            return r

    return None
