"""
Streaming & crash-safe JSON I/O primitives (json_io.py)

Two guarantees the pipeline depends on:

  1. **Bounded memory.** Master aggregation streams the JSONL ledger record by
     record and writes the output array incrementally. Nothing here ever holds
     the whole dataset in RAM, so a 5,000-university ledger costs the same
     working set as a 5-university one.

  2. **No torn writes.** Every whole-file write lands in a sibling ``.tmp`` file
     that is flushed and fsynced before an atomic ``os.replace``. A crash mid-run
     therefore leaves either the previous complete file or the new complete file,
     never a half-serialised one.
"""
import os
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional

logger = logging.getLogger("JsonIO")

__all__ = [
    "atomic_write_text",
    "atomic_write_json",
    "append_jsonl",
    "iter_jsonl",
    "stream_compile_master_json",
]


def _fsync_dir(directory: Path) -> None:
    """Persist a rename in the parent directory's own metadata."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> Path:
    """Write `text` to `path` via a fsynced temp file and an atomic rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, "w", encoding=encoding) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    _fsync_dir(path.parent)
    return path


def atomic_write_json(path: Path, obj: Any, indent: Optional[int] = 2) -> Path:
    """Serialise `obj` to `path` atomically. Use for small/medium objects."""
    return atomic_write_text(path, json.dumps(obj, indent=indent, ensure_ascii=False))


def append_jsonl(path: Path, obj: Any) -> Path:
    """
    Append one record to a JSONL ledger and force it to disk.

    The append is the pipeline's durable record of a completed university, so it
    is fsynced immediately: an unflushed line lost to a crash would make the
    university look unprocessed while its notebook had already been deleted.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")
        f.flush()
        os.fsync(f.fileno())
    return path


def iter_jsonl(path: Path, skip_malformed: bool = True) -> Iterator[Dict[str, Any]]:
    """
    Yield records from a JSONL file one at a time.

    Malformed lines are skipped rather than aborting the sweep -- a single
    truncated line from an old crash must not make the entire ledger unreadable.
    """
    path = Path(path)
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                if not skip_malformed:
                    raise
                logger.warning(f"{path.name}:{line_no}: skipping malformed JSONL record ({e}).")


def _default_record_key(record: Dict[str, Any]) -> Optional[str]:
    """Identity of a university payload, used for last-write-wins dedupe."""
    main = record.get("main_info") or {}
    name = (main.get("name") or "").strip().lower()
    return name or None


def stream_compile_master_json(
    jsonl_path: Path,
    master_path: Path,
    indent: int = 2,
    dedupe: bool = True,
    key_fn: Callable[[Dict[str, Any]], Optional[str]] = _default_record_key,
) -> int:
    """
    Compile the JSONL ledger into the master JSON array in a single streamed pass.

    This replaces the previous per-university "read every record, re-dump every
    record" cycle, which was O(N^2) in both disk I/O and parse cost across a
    batch: university 83 re-parsed and re-wrote the 82 payloads before it.
    Aggregation now happens once, at the end of a run.

    When `dedupe` is set, a re-run of an already-recorded university replaces its
    earlier entry instead of appending a duplicate. The first pass records only
    the *last* line number per key -- keys, not payloads -- so peak memory stays
    proportional to the university count rather than the dataset size.

    Returns:
        Number of records written to `master_path`.
    """
    jsonl_path, master_path = Path(jsonl_path), Path(master_path)

    if not jsonl_path.exists():
        atomic_write_text(master_path, "[]\n")
        return 0

    keep_at_index: Optional[Dict[int, bool]] = None
    if dedupe:
        last_index_for_key: Dict[str, int] = {}
        unkeyed: set = set()
        for idx, record in enumerate(iter_jsonl(jsonl_path)):
            key = key_fn(record)
            if key is None:
                unkeyed.add(idx)          # never drop a record we cannot identify
            else:
                last_index_for_key[key] = idx
        keep_at_index = {i: True for i in last_index_for_key.values()}
        keep_at_index.update({i: True for i in unkeyed})

    master_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = master_path.with_name(master_path.name + ".tmp")
    pad = " " * indent
    written = 0

    with open(tmp_path, "w", encoding="utf-8") as out:
        out.write("[\n")
        for idx, record in enumerate(iter_jsonl(jsonl_path)):
            if keep_at_index is not None and idx not in keep_at_index:
                continue
            if written:
                out.write(",\n")
            # Indent each record's body to match json.dump(..., indent=2) output
            # so the file stays byte-comparable with what consumers already parse.
            body = json.dumps(record, indent=indent, ensure_ascii=False)
            out.write(pad + body.replace("\n", "\n" + pad))
            written += 1
        out.write("\n]\n" if written else "]\n")
        out.flush()
        os.fsync(out.fileno())

    os.replace(tmp_path, master_path)
    _fsync_dir(master_path.parent)
    return written
