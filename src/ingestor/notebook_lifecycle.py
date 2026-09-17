import asyncio
import logging
from typing import Any, List, Optional

from notebooklm import NotebookLMClient

from src.config import config
from src.logger.notebook_logger import log_notebook_created
from src.ingestor.readiness_polling import _extract_id

logger = logging.getLogger("Ingest.lifecycle")


async def purge_notebook_sources(
    client: NotebookLMClient, notebook_id: str, sources: Optional[List[Any]] = None
) -> int:
    """Removes all sources from a reusable worker notebook to prepare for the next university."""
    try:
        if sources is None:
            list_fn = getattr(getattr(client, "sources", None), "list", None)
            if not callable(list_fn):
                return 0
            res = list_fn(notebook_id)
            sources = await res if asyncio.iscoroutine(res) else res
        if not sources:
            return 0

        sem = asyncio.Semaphore(10)
        deleted_count = 0

        async def _delete_one(s):
            nonlocal deleted_count
            s_id = getattr(s, "id", None) or getattr(s, "source_id", None)
            if not s_id or not hasattr(client.sources, "delete"):
                return
            async with sem:
                try:
                    del_fn = client.sources.delete(notebook_id, s_id)
                    if asyncio.iscoroutine(del_fn):
                        await del_fn
                    deleted_count += 1
                except Exception as e:
                    logger.debug(f"Failed to delete source {s_id} from {notebook_id}: {e}")

        await asyncio.gather(*(_delete_one(s) for s in sources))
        logger.info(f"Purged {deleted_count} sources from reusable worker notebook {notebook_id}.")
        return deleted_count
    except Exception as e:
        logger.warning(f"Could not purge sources in notebook {notebook_id}: {e}")
        return 0


async def _find_or_create_notebook(client: NotebookLMClient, title: str, uni_slug: Optional[str] = None) -> str:
    """
    Reuse an existing notebook with this title, else create one.

    In reusable mode (config.notebook_mode == 'reusable'), routes to config.worker_notebook_title.
    Reuse matters for resumability: a run that crashed after uploading 40 of 60
    sources must not leave an orphan notebook consuming a workspace slot and then
    create a second one on retry.
    """
    if getattr(config, "notebook_mode", "ephemeral") == "reusable":
        title = getattr(config, "worker_notebook_title", "Education_Counselor_Worker_DB")

    try:
        for nb in await client.notebooks.list():
            if getattr(nb, "title", None) == title:
                nb_id = _extract_id(nb)
                if nb_id:
                    logger.info(f"Reusing existing notebook '{title}' ({nb_id})")
                    log_notebook_created(nb_id, title, uni_slug)
                    return nb_id
    except Exception as e:
        # A listing failure is not fatal -- we can still create -- but it must be
        # visible, since it is the only thing standing between us and duplicates.
        logger.warning(f"Could not list existing notebooks ({e}); proceeding to create '{title}'.")

    nb = await client.notebooks.create(title=title)
    nb_id = _extract_id(nb)
    if not nb_id:
        raise RuntimeError(f"notebooks.create returned no usable id for '{title}'")
    logger.info(f"Created notebook '{title}' ({nb_id})")
    log_notebook_created(nb_id, title, uni_slug)
    return nb_id


