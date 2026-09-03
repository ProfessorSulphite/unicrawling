"""
Workspace management: archiving a previous run's outputs before a full rerun.

Leaf layer. Destructive, so it is deliberately isolated from the orchestration
that calls it: the copytree/rmtree pair and the state-database reset are the two
operations in this repository that can lose a completed run, and they should be
readable in one screen rather than buried in a batch driver.
"""
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.config import config


def _holds_any_file(directory: Path) -> bool:
    """True when there is at least one real file anywhere under `directory`."""
    return directory.exists() and any(p.is_file() for p in directory.rglob("*"))


def _unused_backup_dir(timestamp: str) -> Path:
    """A backup path that does not exist yet, disambiguating same-second runs."""
    base = config.base_dir / "data" / f"outputs_backup_{timestamp}"
    if not base.exists():
        return base
    for n in range(2, 1000):
        candidate = base.with_name(f"{base.name}_{n}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find an unused backup path beside {base}")


def backup_existing_outputs() -> Optional[Path]:
    """
    Move data/outputs aside into a timestamped sibling and reset the state DB.

    Returns the backup directory, or None when there was nothing to archive --
    an empty outputs directory counts as nothing, so a first run does not leave
    an empty backup behind.

    The state database is reset alongside the outputs on purpose: leaving it in
    place would leave every university marked "completed" while the payloads
    those rows point at have just been moved, so the next run would skip
    everything and produce nothing. The two have to move together.
    """
    archived: Optional[Path] = None
    # "Has anything in it" means a FILE somewhere beneath, not an entry at the
    # top: ensure_directories() creates uni_outputs/ and all_uni_outputs/ on
    # import, so a pristine workspace always has entries and never has data.
    if _holds_any_file(config.data_outputs_dir):
        # Second-resolution timestamps collide, and copytree raises
        # FileExistsError on a collision -- so two backups in the same second
        # crashed the run mid-archive. A suffix is cheaper than losing the run.
        backup_dir = _unused_backup_dir(datetime.now().strftime("%Y%m%d_%H%M%S"))
        shutil.copytree(config.data_outputs_dir, backup_dir)
        shutil.rmtree(config.data_outputs_dir)
        archived = backup_dir

    # -wal and -shm alongside the database itself: deleting only the main file
    # leaves SQLite able to recover the old contents from the write-ahead log.
    for ext in ("", "-wal", "-shm"):
        f_path = Path(str(config.state_db_path) + ext)
        if f_path.exists():
            try:
                f_path.unlink()
            except OSError:
                pass

    config.data_outputs_dir.mkdir(parents=True, exist_ok=True)
    config.outputs_uni_outputs_dir.mkdir(parents=True, exist_ok=True)
    (config.data_outputs_dir / "country_outputs").mkdir(parents=True, exist_ok=True)
    return archived
