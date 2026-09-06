"""
The read layer: every inspector command reaches the corpus through here.

`iter_all_records` runs each payload through the normalizer on the way out, so
retired bucket names, retired degree-level values and the pre-C18 singular
deadline key are migrated on read. That is why a schema change has to keep a
migration path: this is the door every stored file comes back through.

Both lookup routes go through `_normalized`, so the same stored file reads the
same way whether it was found by filename or by the fallback scan.

Reads only. Nothing in this module writes, exports, or triggers a run.
"""
import json
from typing import Any, Dict, Iterator, List, Optional

from src.config import config
from src.extractor.normalizers.runner import normalize_universal_payload
from src.utilities.json_io import iter_jsonl, record_key


def _normalized(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    The single point every stored file passes through on its way back in.

    Both lookup routes call it. They used to disagree: find_university_record's
    filename fast path returned the file verbatim, so a pre-C17 payload reported
    zero programmes to `inspect` and `diff` -- its programmes were still filed
    under the retired bucket names -- while `search` and `export`, which stream
    through iter_all_records, saw every one of them.
    """
    return normalize_universal_payload(data)


def iter_all_records() -> Iterator[Dict[str, Any]]:
    """
    Yield the CURRENT normalized payload for each university, one at a time.

    The ledger is append-only: re-running a university appends a row rather than
    replacing one, so a university with seven runs has seven rows and only the
    last is current. This function used to keep the FIRST row per name while
    stream_compile_master_json keeps the LAST -- two opposite rules over one
    file. The master JSON therefore held the newest payload while every
    inspector command reading through here held the oldest.

    Observed on 2026-09-05: ITU's newest run extracted 20 programmes and wrote
    them to the master JSON, while `cli.py audit` reported 13 and flagged a
    bucket-contamination bug fixed two days earlier -- it was auditing the run of
    2026-09-03. The Supabase readiness gate reads the same door, so the push was
    being judged on a superseded payload.

    Identity comes from utilities.json_io.record_key, the same function the
    compiler uses, so the two cannot drift apart again.

    Still streaming. Two passes over the ledger, mirroring the compiler: the
    first records only the last line number per identity, the second yields the
    records at those lines. Peak memory stays proportional to the number of
    universities rather than the size of the corpus, which is the guarantee this
    generator exists for. `load_all_records()` remains the eager list form.
    """
    seen: set = set()

    # 1. The master JSONL ledger, last row per university.
    if config.output_jsonl_path.exists():
        current_at: Dict[str, int] = {}
        unkeyed: set = set()
        for index, record in enumerate(iter_jsonl(config.output_jsonl_path)):
            key = record_key(record)
            if key is None:
                # Never drop a record we cannot identify -- the same rule the
                # compiler applies, for the same reason.
                unkeyed.add(index)
            else:
                current_at[key] = index

        keep = set(current_at.values()) | unkeyed
        for index, record in enumerate(iter_jsonl(config.output_jsonl_path)):
            if index not in keep:
                continue
            key = record_key(record)
            if key is not None:
                seen.add(key)
            yield _normalized(record)

    # 2. Per-slug JSON files, for universities the ledger has no row for.
    # Sorted so the corpus reads in a stable order regardless of the filesystem.
    if config.outputs_uni_outputs_dir.exists():
        for path in sorted(config.outputs_uni_outputs_dir.glob("*.json")):
            try:
                with open(path, "r", encoding="utf-8") as file_obj:
                    data = json.load(file_obj)
            except Exception:
                continue
            key = record_key(data)
            if key is not None and key in seen:
                continue
            if key is not None:
                seen.add(key)
            yield _normalized(data)


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
                        return _normalized(json.load(file_obj))
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
