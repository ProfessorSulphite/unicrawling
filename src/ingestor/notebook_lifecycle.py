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
    patch_notebooklm_rpc_size_limit()
    return nb_id


def patch_notebooklm_rpc_size_limit(max_bytes: int = 200 * 1024 * 1024) -> None:
    """Widen notebooklm-py's internal MAX_RPC_RESPONSE_BYTES guard to 200MB."""
    import functools
    try:
        import notebooklm._kernel
        import notebooklm._streaming_post

        orig_stream = notebooklm._streaming_post.stream_post_with_size_cap
        if getattr(orig_stream, "_is_patched", False):
            return

        @functools.wraps(orig_stream)
        async def patched_stream_post(client, url, body, headers, timeout=None, max_bytes=max_bytes):
            return await orig_stream(client, url, body, headers, timeout=timeout, max_bytes=max_bytes)

        patched_stream_post._is_patched = True
        notebooklm._streaming_post.stream_post_with_size_cap = patched_stream_post
        notebooklm._kernel.stream_post_with_size_cap = patched_stream_post
        logger.debug(f"Patched notebooklm-py streaming response buffer limit to {max_bytes / (1024 * 1024):.0f}MB.")
    except Exception as e:
        logger.warning(f"Could not patch notebooklm-py RPC size limit: {e}")



