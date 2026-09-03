"""
Currency labelling and tuition-fee defaults.

Deliberately *labels* currency and never converts it (Finding 8): a converted
figure would be stale the day after it was written and indistinguishable from a
figure the university actually published. Tuition stays in whatever currency the
source stated.
"""
from typing import Any, Dict, Optional


# Global country to currency fallback mapping
COUNTRY_CURRENCY_MAP = {
    "germany": "EUR",
    "france": "EUR",
    "italy": "EUR",
    "spain": "EUR",
    "netherlands": "EUR",
    "finland": "EUR",
    "austria": "EUR",
    "belgium": "EUR",
    "switzerland": "CHF",
    "united states": "USD",
    "usa": "USD",
    "united kingdom": "GBP",
    "uk": "GBP",
    "canada": "CAD",
    "australia": "AUD",
    "china": "CNY",
    "japan": "JPY",
    "pakistan": "PKR",
}


def resolve_universal_currency(tuition_str: Optional[str], country: str) -> str:
    """
    Detects exact currency from tuition text or country locale.
    """
    if tuition_str:
        s = tuition_str.upper()
        if "EUR" in s or "€" in s or "EURO" in s:
            return "EUR"
        if "USD" in s or "$" in s or "DOLLAR" in s:
            return "USD"
        if "GBP" in s or "£" in s or "POUND" in s:
            return "GBP"
        if "CHF" in s:
            return "CHF"
        if "PKR" in s or "RS" in s or "RUPEE" in s:
            return "PKR"
        if "CAD" in s:
            return "CAD"
        if "AUD" in s:
            return "AUD"

    country_key = (country or "Pakistan").strip().lower()
    return COUNTRY_CURRENCY_MAP.get(country_key, "EUR" if "europe" in country_key else "PKR")


def apply_currency_and_tuition(prog: Dict[str, Any], country: str) -> Dict[str, Any]:
    """
    Steps 1 and 2 of the former normalize_universal_program, extracted verbatim in
    C16: resolve the currency label, then fill tuition and application fee when the
    model returned nothing.

    Split out from the eligibility rules so the two can be read -- and revised in
    C19 -- independently. Both fee strings written here are invented rather than
    sourced; see decision D2.
    """
    tuition = prog.get("tuition_fee")
    currency = prog.get("currency")

    # 1. Resolve Currency
    resolved_currency = resolve_universal_currency(str(tuition or ""), country)
    if not currency or currency == "PKR" and country.lower() in ("germany", "france", "united states", "usa", "uk", "united kingdom", "switzerland"):
        prog["currency"] = resolved_currency

    # 2. Tuition Fee Normalization
    country_lower = country.lower()
    is_empty_tuition = not tuition or str(tuition).strip().lower() in ("null", "none", "n/a", "0", "")
    
    if is_empty_tuition:
        if country_lower in ("germany", "finland", "norway", "austria"):
            prog["tuition_fee"] = "Tuition Free (Semester Contribution applies)"
            if not prog.get("application_fee"):
                prog["application_fee"] = "Uni-Assist €75 / Free Direct Application"
        else:
            prog["tuition_fee"] = "Refer to Official Tuition Portal"

    if not prog.get("application_fee"):
        prog["application_fee"] = "Standard University Application Fee"
    return prog
