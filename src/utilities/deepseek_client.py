"""
DeepSeek-V4.1-Flash API Client (deepseek_client.py).

Provides:
1. Fast financial normalization: parsing raw unstructured tuition fee strings into
   standardized currency objects with USD-equivalent amounts for cross-currency search.
2. Zero-quota direct field extraction from supplementary scraped pages (portals, fees, deadlines)
   in ~300ms using non-thinking mode.
3. Offline deterministic regex fallback ensuring 100% operational continuity when API key is unset.
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx

from src.config import config
from src.utilities.schema import NormalizedTuition, ProgramItem

logger = logging.getLogger("DeepSeekClient")

# Base FX exchange rates relative to 1.0 USD (calibrated baseline)
BASE_FX_RATES: Dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.30,
    "CHF": 1.15,
    "CAD": 0.74,
    "AUD": 0.65,
    "PKR": 0.0036,   # ~278 PKR per USD
    "INR": 0.012,    # ~83 INR per USD
    "CNY": 0.14,     # ~7.1 CNY per USD
    "AED": 0.272,
    "QAR": 0.274,
    "SAR": 0.266,
    "JPY": 0.0068,
}

# Regex for offline extraction
CURRENCY_SYMBOLS: Dict[str, str] = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "rs": "PKR",
    "pkr": "PKR",
    "inr": "INR",
    "cad": "CAD",
    "aud": "AUD",
    "chf": "CHF",
    "usd": "USD",
    "eur": "EUR",
    "gbp": "GBP",
}

INTERVAL_PATTERNS = [
    (r"\b(per\s+year|annual|annually|/year|/\s*yr)\b", "annual"),
    (r"\b(per\s+semester|semester|sem|/semester|/\s*sem)\b", "semester"),
    (r"\b(per\s+term|term|/term)\b", "semester"),
    (r"\b(per\s+credit|credit\s+hour|/credit)\b", "credit"),
    (r"\b(total|entire\s+program|full\s+degree)\b", "total"),
]


def is_deepseek_available() -> bool:
    """Return True if a DeepSeek API key is configured in settings or environment."""
    key = config.deepseek_api_key or os.getenv("DEEPSEEK_API_KEY", "")
    return bool(key.strip())


def _offline_normalize_fee(raw_fee: str, default_currency: Optional[str] = None) -> NormalizedTuition:
    """
    Deterministic rule-based fee parser for offline testing or when API is unavailable.
    """
    if not raw_fee or not raw_fee.strip():
        return NormalizedTuition(raw_fee=raw_fee)

    clean_raw = raw_fee.strip()
    lowered = clean_raw.lower()

    # Detect currency
    currency = default_currency or "USD"
    for symbol, curr in CURRENCY_SYMBOLS.items():
        if symbol in lowered:
            currency = curr
            break

    # Detect interval
    interval = "annual"
    for pat, matched_int in INTERVAL_PATTERNS:
        if re.search(pat, lowered):
            interval = matched_int
            break

    # Extract numeric amount
    numbers = re.findall(r"\d[\d,]*", clean_raw)
    amount: Optional[float] = None
    if numbers:
        try:
            amount = float(numbers[0].replace(",", ""))
        except ValueError:
            amount = None

    normalized_usd: Optional[float] = None
    if amount is not None:
        rate = BASE_FX_RATES.get(currency.upper(), 1.0)
        normalized_usd = round(amount * rate, 2)

    return NormalizedTuition(
        amount=amount,
        currency=currency.upper(),
        interval=interval,
        normalized_usd=normalized_usd,
        raw_fee=clean_raw,
    )


async def normalize_tuition_batch(
    programs: List[ProgramItem],
    default_currency: Optional[str] = None,
) -> List[ProgramItem]:
    """
    Normalize tuition fees across a batch of programs for a university.
    Uses DeepSeek-V4.1-Flash in non-thinking JSON mode when available, with
    instant fallback to the offline normalizer.
    """
    if not programs:
        return programs

    # If DeepSeek is not available, use offline normalizer
    if not is_deepseek_available():
        for p in programs:
            if p.tuition_fee and not p.tuition_fee_normalized:
                p.tuition_fee_normalized = _offline_normalize_fee(p.tuition_fee, p.currency or default_currency)
        return programs

    items_to_normalize = []
    for idx, p in enumerate(programs):
        if p.tuition_fee:
            items_to_normalize.append({
                "index": idx,
                "degree_name": p.name,
                "raw_fee": p.tuition_fee,
                "currency_hint": p.currency or default_currency or "",
            })

    if not items_to_normalize:
        return programs

    api_key = (config.deepseek_api_key or os.getenv("DEEPSEEK_API_KEY", "")).strip()
    base_url = (config.deepseek_base_url or "https://api.deepseek.com").rstrip("/")
    endpoint = f"{base_url}/chat/completions"

    system_prompt = (
        "You are a financial data normalization engine for an international university admissions system.\n"
        "Parse raw tuition fee descriptions into structured financial objects with USD equivalents.\n"
        "Reference FX baseline: USD=1.0, EUR=1.08, GBP=1.30, CAD=0.74, AUD=0.65, PKR=0.0036, INR=0.012, CNY=0.14, CHF=1.15.\n"
        "Respond ONLY with a JSON object containing a 'normalized' array: "
        "{\"normalized\": [{\"index\": int, \"amount\": float, \"currency\": str, \"interval\": \"annual\"|\"semester\"|\"credit\"|\"total\", \"normalized_usd\": float}]}"
    )

    user_prompt = f"Normalize these university program fees:\n{json.dumps(items_to_normalize, indent=2)}"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.deepseek_model or "deepseek-flash",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "temperature": 0.0,
        "max_tokens": 2048,
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                cleaned = content.strip() if content else ""
                if cleaned.startswith("```"):
                    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
                    cleaned = re.sub(r"\s*```$", "", cleaned)
                    cleaned = cleaned.strip()
                parsed_json = json.loads(cleaned)
                normalized_items = parsed_json.get("normalized", [])

                for item in normalized_items:
                    idx = item.get("index")
                    if idx is not None and 0 <= idx < len(programs):
                        programs[idx].tuition_fee_normalized = NormalizedTuition(
                            amount=float(item.get("amount", 0.0)) if item.get("amount") is not None else None,
                            currency=str(item.get("currency", "")).upper() or None,
                            interval=str(item.get("interval", "annual")).lower(),
                            normalized_usd=float(item.get("normalized_usd", 0.0)) if item.get("normalized_usd") is not None else None,
                            raw_fee=programs[idx].tuition_fee,
                        )
                logger.info(f"DeepSeek normalized tuition fees for {len(normalized_items)} programs.")
                return programs
            else:
                logger.warning(f"DeepSeek API returned HTTP {resp.status_code}: {resp.text[:120]}. Using offline fallback.")
    except Exception as e:
        logger.warning(f"DeepSeek normalization call failed ({type(e).__name__}: {e}). Using offline fallback.")

    # Fallback to offline normalizer
    for p in programs:
        if p.tuition_fee and not p.tuition_fee_normalized:
            p.tuition_fee_normalized = _offline_normalize_fee(p.tuition_fee, p.currency or default_currency)

    return programs


async def extract_field_from_text(
    page_text: str,
    field_name: str,
    uni_name: str = "",
    context: str = "",
) -> Optional[str]:
    """
    Extract a single missing fact (application portal URL, tuition table, deadline)
    from scraped page text using DeepSeek-V4.1-Flash non-thinking mode in ~300ms.
    """
    if not is_deepseek_available() or not page_text:
        return None

    api_key = (config.deepseek_api_key or os.getenv("DEEPSEEK_API_KEY", "")).strip()
    base_url = (config.deepseek_base_url or "https://api.deepseek.com").rstrip("/")
    endpoint = f"{base_url}/chat/completions"

    system_prompt = (
        "You are an accurate, zero-hallucination data extraction agent for university counseling.\n"
        "Extract the requested field strictly from the provided text. If the field cannot be found, output null.\n"
        "Output JSON only: {\"extracted_value\": str | null, \"quote\": str | null}"
    )

    truncated_text = page_text[:12000]  # Safe chunk
    user_prompt = (
        f"University: {uni_name}\n"
        f"Target Field: {field_name}\n"
        f"Context: {context}\n"
        f"Document Content:\n{truncated_text}\n"
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.deepseek_model or "deepseek-flash",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "temperature": 0.0,
        "max_tokens": 512,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                cleaned = content.strip() if content else ""
                if cleaned.startswith("```"):
                    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
                    cleaned = re.sub(r"\s*```$", "", cleaned)
                    cleaned = cleaned.strip()
                result = json.loads(cleaned)
                val = result.get("extracted_value")
                return str(val).strip() if val else None
    except Exception as e:
        logger.warning(f"DeepSeek direct field extraction failed ({field_name}): {e}")

    return None
