"""
NotebookLM notebook provisioning.

Deletion currently lives in extractor/crawlers (delete_notebook_after_success);
C15 relocates it here so the whole lifecycle sits in one module.
"""
import logging
from typing import Optional

from notebooklm import NotebookLMClient

from src.config import config
from src.logger.notebook_logger import log_notebook_created
from src.ingestor.readiness_polling import _extract_id

logger = logging.getLogger("Ingest.lifecycle")

async def _find_or_create_notebook(client: NotebookLMClient, title: str, uni_slug: Optional[str] = None) -> str:
    """
    Reuse an existing notebook with this title, else create one.

    Reuse matters for resumability: a run that crashed after uploading 40 of 60
    sources must not leave an orphan notebook consuming a workspace slot and then
    create a second one on retry.
    """
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


