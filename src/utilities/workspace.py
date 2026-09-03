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


def backup_existing_outputs() -> Optional[Path]:
    """
    Move data/outputs aside into a timestamped sibling and reset the state DB.

    Returns the backup directory, or None when there was nothing to archive.

    The state database is reset alongside the outputs on purpose: leaving it in
    place would leave every university marked "completed" while the payloads
    those rows point at have just been moved, so the next run would skip
    everything and produce nothing. The two have to move together.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = config.base_dir / "data" / f"outputs_backup_{timestamp}"

    archived: Optional[Path] = None
    if config.data_outputs_dir.exists():
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
