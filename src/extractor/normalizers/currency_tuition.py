"""
Currency labelling. Never conversion, and since C19 never invention.

Two rules, in order of evidence:

  1. The fee text itself. "€1,500 per semester" is the university saying EUR.
  2. Failing that, the university's country -- a fee a Swiss university publishes
     is in CHF whether or not it printed the symbol.

Anything else resolves to None. Before C19 this module also wrote fee strings
into empty fields ("Refer to Official Tuition Portal", "Standard University
Application Fee", "Tuition Free (Semester Contribution applies)" for a handful
of European countries). None of it was sourced, and the last one was an outright
claim about money. They are gone; an unstated fee is now null, which is what the
inspector's empty-field audit needs in order to mean anything.

Conversion remains out of the question (Finding 8): a converted figure is stale
the day after it is written and indistinguishable from one the university
actually published.
"""
import re
from typing import Any, Dict, Optional

# Country to currency. A label for a figure the university published in its own
# currency -- not a conversion, and not a guess about the amount.
COUNTRY_CURRENCY_MAP = {
    "germany": "EUR",
    "france": "EUR",
    "italy": "EUR",
    "spain": "EUR",
    "netherlands": "EUR",
    "finland": "EUR",
    "austria": "EUR",
    "belgium": "EUR",
    "ireland": "EUR",
    "portugal": "EUR",
    "greece": "EUR",
    "switzerland": "CHF",
    "united states": "USD",
    "usa": "USD",
    "united states of america": "USD",
    "united kingdom": "GBP",
    "uk": "GBP",
    "canada": "CAD",
    "australia": "AUD",
    "new zealand": "NZD",
    "china": "CNY",
    "japan": "JPY",
    "india": "INR",
    "pakistan": "PKR",
    "bangladesh": "BDT",
    "sri lanka": "LKR",
    "kenya": "KES",
    "tanzania": "TZS",
    "uganda": "UGX",
    "nigeria": "NGN",
    "south africa": "ZAR",
    "egypt": "EGP",
    "turkey": "TRY",
    "malaysia": "MYR",
    "singapore": "SGD",
    "saudi arabia": "SAR",
    "united arab emirates": "AED",
    "uae": "AED",
    "qatar": "QAR",
    "sweden": "SEK",
    "norway": "NOK",
    "denmark": "DKK",
    "poland": "PLN",
    "czechia": "CZK",
    "czech republic": "CZK",
    "hungary": "HUF",
    "russia": "RUB",
    "brazil": "BRL",
    "mexico": "MXN",
    "south korea": "KRW",
    "korea": "KRW",
    "indonesia": "IDR",
    "thailand": "THB",
    "philippines": "PHP",
    "vietnam": "VND",
}

# Markers matched in the fee text. Symbols match anywhere; alphabetic codes and
# words are matched on WORD BOUNDARIES -- the pre-C19 version tested `"RS" in s`
# against an uppercased string, so any fee mentioning a "COURSE" was labelled
# PKR. Same substring-versus-token defect the link filter and the degree mapper
# each had to fix.
_CURRENCY_MARKERS = (
    ("EUR", (r"€", r"\bEUR\b", r"\bEUROS?\b")),
    ("GBP", (r"£", r"\bGBP\b", r"\bPOUNDS?\b", r"\bSTERLING\b")),
    ("CHF", (r"\bCHF\b", r"\bFRANCS?\b")),
    ("PKR", (r"\bPKR\b", r"\bRS\b", r"\bRS\.", r"\bRUPEES?\b")),
    ("INR", (r"₹", r"\bINR\b")),
    ("CAD", (r"\bCAD\b", r"\bC\$")),
    ("AUD", (r"\bAUD\b", r"\bA\$")),
    ("JPY", (r"¥", r"\bJPY\b", r"\bYEN\b")),
    ("CNY", (r"\bCNY\b", r"\bRMB\b", r"\bYUAN\b")),
    ("USD", (r"\bUSD\b", r"\bDOLLARS?\b", r"\$")),
)

_COMPILED_MARKERS = tuple(
    (code, tuple(re.compile(p) for p in patterns)) for code, patterns in _CURRENCY_MARKERS
)


def resolve_universal_currency(
    tuition_str: Optional[str], country: Optional[str]
) -> Optional[str]:
    """Label the currency of a published fee, or None when nothing indicates it.

    None is the honest answer for a university in a country the map does not
    cover, and there are far more of those than there are entries here. Before
    C19 the tail of this function fell through to PKR for the entire world
    outside Europe, which quietly asserted that a Kenyan fee was in rupees.
    """
    if tuition_str:
        text = str(tuition_str).upper()
        for code, patterns in _COMPILED_MARKERS:
            if any(p.search(text) for p in patterns):
                return code

    if country:
        return COUNTRY_CURRENCY_MAP.get(country.strip().lower())
    return None


def apply_currency_and_tuition(
    prog: Dict[str, Any], country: Optional[str]
) -> Dict[str, Any]:
    """Label the fee's currency. Fill nothing else."""
    resolved = resolve_universal_currency(prog.get("tuition_fee"), country)

    # Only ever written when we resolved something. An existing label the source
    # supplied is never overwritten -- it outranks any inference from country.
    if not str(prog.get("currency") or "").strip() and resolved:
        prog["currency"] = resolved

    # The fee fields are made present but never filled. The inspector counts
    # empty fields by reading them, so an absent key and a null one must not be
    # two different things.
    prog.setdefault("currency", None)
    prog.setdefault("tuition_fee", None)
    prog.setdefault("application_fee", None)
    return prog
