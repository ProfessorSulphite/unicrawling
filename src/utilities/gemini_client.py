"""
Gemini API Client (gemini_client.py).

Provides:
1. Key discovery that is indifferent to how many keys exist. One key is the
   normal case; several are rotated round-robin. Nothing in the pipeline
   requires a second key, a second account, or a specific variable name.
2. Rate-limited async dispatch with retry on HTTP 429.
3. GeminiQuotaError for daily quota / permission exhaustion.
4. is_gemini_available(), so engine auto-selection never picks an engine that
   cannot actually run.

Keys are read, in order, from:
    config.gemini_api_keys, then the environment -- GEMINI_API_KEYS,
    GEMINI_API_KEY, GOOGLE_API_KEY.
The first source that yields anything wins, and each may hold one key or several
separated by commas or whitespace.
"""
import asyncio
import importlib.util
import itertools
import logging
import os
import time
from typing import Any, List, Optional, Sequence, Tuple, Type

from pydantic import BaseModel

from src.config import config

logger = logging.getLogger("GeminiClient")

# Checked in order; the first one holding a value decides. GEMINI_API_KEYS is
# listed first so a multi-key setup can coexist with a single-key one left in
# the environment by an earlier run.
_KEY_ENV_VARS: Tuple[str, ...] = ("GEMINI_API_KEYS", "GEMINI_API_KEY", "GOOGLE_API_KEY")


class GeminiQuotaError(RuntimeError):
    """Raised when Gemini keys are rate-limited past retry, or the quota is spent."""


_GEMINI_EXHAUSTED = False

# Round-robin state. Keyed by the key tuple it was built from, so that changing
# the configured keys (which tests do constantly) rebuilds the cycle instead of
# handing out keys that are no longer configured.
_key_cycle: Optional[itertools.cycle] = None
_key_cycle_source: Tuple[str, ...] = ()

# Request pacing. Gemini's free tier is billed per key per minute, so N keys buy
# N times the throughput; the gate below spaces requests accordingly.
_rate_lock: Optional[asyncio.Lock] = None
_last_request_at: float = 0.0


def _split_keys(raw: str) -> List[str]:
    """Split one env value into keys on commas or whitespace, preserving order."""
    if not raw:
        return []
    out: List[str] = []
    for chunk in raw.replace("\n", ",").replace(" ", ",").split(","):
        key = chunk.strip()
        if key and key not in out:
            out.append(key)
    return out


def _get_api_keys() -> List[str]:
    """Every configured Gemini key, in priority order. One key is a valid setup."""
    keys = _split_keys(getattr(config, "gemini_api_keys", "") or "")
    if keys:
        return keys
    for var in _KEY_ENV_VARS:
        keys = _split_keys(os.getenv(var, ""))
        if keys:
            return keys
    return []


def _sdk_available() -> bool:
    """True when google-genai is importable."""
    return importlib.util.find_spec("google.genai") is not None


def is_gemini_available() -> bool:
    """
    True when Gemini can actually serve a request: at least one key, the SDK
    installed, and the quota not already spent in this process.

    The SDK check matters for `--engine auto`: selecting an engine whose client
    library is missing would fail every university instead of falling through.
    """
    if _GEMINI_EXHAUSTED:
        return False
    if not _get_api_keys():
        return False
    return _sdk_available()


def mark_gemini_exhausted() -> None:
    """Mark Gemini as exhausted for the current process session."""
    global _GEMINI_EXHAUSTED
    _GEMINI_EXHAUSTED = True


def reset_gemini_exhausted() -> None:
    """Reset the exhausted state (used by tests and between runs)."""
    global _GEMINI_EXHAUSTED, _key_cycle, _key_cycle_source, _last_request_at
    _GEMINI_EXHAUSTED = False
    _key_cycle = None
    _key_cycle_source = ()
    _last_request_at = 0.0


def _next_key() -> str:
    """Round-robin key selection. With a single key this returns it every time."""
    global _key_cycle, _key_cycle_source
    keys = _get_api_keys()
    if not keys:
        raise GeminiQuotaError(
            "No Gemini API key configured. Set GEMINI_API_KEY (one key) or "
            "GEMINI_API_KEYS (comma-separated) in the environment or .env."
        )
    signature = tuple(keys)
    if _key_cycle is None or _key_cycle_source != signature:
        _key_cycle = itertools.cycle(keys)
        _key_cycle_source = signature
    return next(_key_cycle)


def _min_request_interval() -> float:
    """Seconds to leave between requests so the free-tier per-minute cap holds."""
    rpm = max(1, int(getattr(config, "gemini_rpm_per_key", 15) or 15))
    keys = max(1, len(_get_api_keys()))
    return 60.0 / float(rpm * keys)


async def _throttle() -> None:
    """Space consecutive requests; usually a no-op because crawling is slower."""
    global _rate_lock, _last_request_at
    if _rate_lock is None:
        _rate_lock = asyncio.Lock()
    async with _rate_lock:
        wait = _min_request_interval() - (time.monotonic() - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = time.monotonic()


def _status_code(error: BaseException) -> Optional[int]:
    """HTTP status behind an SDK error, when it exposes one."""
    for attr in ("code", "status_code"):
        value = getattr(error, attr, None)
        if isinstance(value, int):
            return value
    return None


def _is_rate_limit(error: BaseException) -> bool:
    if _status_code(error) == 429:
        return True
    text = str(error).lower()
    return "429" in text or "resource_exhausted" in text or "rate limit" in text


def _is_quota_or_auth(error: BaseException) -> bool:
    if _status_code(error) in (401, 403):
        return True
    text = str(error).lower()
    return (
        "403" in text
        or "401" in text
        or "permission_denied" in text
        or "api key not valid" in text
        or "quota" in text
    )


async def gemini_generate_json(
    user_prompt: str,
    system_instruction: str,
    response_schema: Type[BaseModel],
    model: Optional[str] = None,
    max_retries: int = 3,
    timeout: Optional[float] = None,
) -> Any:
    """
    Call Gemini with native Pydantic schema enforcement and return the parsed
    object (or, when the SDK could not parse it, the raw JSON text).

    Retries rate limits with exponential backoff, rotating to the next key on
    each attempt. Raises GeminiQuotaError once retries are spent, or immediately
    on an auth/quota rejection, so the orchestrator can fall back or fail fast.
    """
    try:
        from google import genai
    except ImportError as exc:  # pragma: no cover - guarded by is_gemini_available()
        raise GeminiQuotaError(
            f"google-genai is not installed; cannot run the Gemini engine ({exc})."
        ) from exc

    target_model = model or getattr(config, "gemini_model", "") or "gemini-3.6-flash"
    request_config: dict = {
        "response_mime_type": "application/json",
        "response_schema": response_schema,
        "system_instruction": system_instruction,
        "temperature": 0.0,
    }
    if timeout:
        # The SDK takes milliseconds here.
        request_config["http_options"] = {"timeout": int(timeout * 1000)}

    delay = 2.0
    last_error: Optional[BaseException] = None

    for attempt in range(1, max_retries + 1):
        api_key = _next_key()
        await _throttle()
        try:
            client = genai.Client(api_key=api_key)
            response = await client.aio.models.generate_content(
                model=target_model,
                contents=user_prompt,
                config=request_config,
            )
            parsed = getattr(response, "parsed", None)
            if parsed is not None:
                return parsed
            # Schema enforcement is best-effort: when the SDK cannot coerce the
            # reply, hand the raw text back so the caller can parse leniently
            # rather than losing a whole university to one strict field.
            return getattr(response, "text", None)

        except Exception as exc:  # noqa: BLE001 - classified immediately below
            last_error = exc
            if _is_rate_limit(exc):
                if attempt == max_retries:
                    mark_gemini_exhausted()
                    raise GeminiQuotaError(
                        f"Gemini rate limited after {max_retries} attempts: {exc}"
                    ) from exc
                logger.warning(
                    f"Gemini rate limited (attempt {attempt}/{max_retries}); retrying in {delay:.1f}s."
                )
                await asyncio.sleep(delay)
                delay *= 2.0
                continue
            if _is_quota_or_auth(exc):
                mark_gemini_exhausted()
                raise GeminiQuotaError(f"Gemini quota/permission error: {exc}") from exc
            raise

    raise GeminiQuotaError(f"Gemini request failed: {last_error}")


def describe_gemini_keys() -> str:
    """One-line, non-secret summary of the key setup, for run banners."""
    count = len(_get_api_keys())
    if count == 0:
        return "no Gemini key configured"
    return f"{count} Gemini key{'s' if count != 1 else ''} configured"


__all__: Sequence[str] = (
    "GeminiQuotaError",
    "describe_gemini_keys",
    "gemini_generate_json",
    "is_gemini_available",
    "mark_gemini_exhausted",
    "reset_gemini_exhausted",
)
