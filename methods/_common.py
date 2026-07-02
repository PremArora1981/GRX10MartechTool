"""Shared helpers for the estimation methods.

These are pure, dependency-light utilities reused across the ``methods/``
package: period/year parsing, country-code matching (the raw tables carry a
mix of ISO-3, ISO-2 and plain country names depending on the connector that
landed them), currency-figure extraction for the text-mining methods, HS-code
normalisation, USD→USD-millions conversion, and an assumptions-ledger lookup.

Nothing here fabricates data — every helper either returns a value derived from
its inputs or ``None``/empty when the inputs do not support a conclusion.
"""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection

# --------------------------------------------------------------------------- #
# USD-millions scale                                                          #
# --------------------------------------------------------------------------- #
_USD_PER_MILLION = Decimal("1000000")
_CENTS = Decimal("0.01")


def usd_to_musd(value_usd: Any) -> Decimal | None:
    """Convert an absolute USD figure to USD millions, rounded to cents.

    Returns ``None`` when ``value_usd`` is missing or non-numeric so callers
    can skip rather than emit a bogus zero.
    """
    if value_usd is None:
        return None
    try:
        dec = value_usd if isinstance(value_usd, Decimal) else Decimal(str(value_usd))
    except (ValueError, ArithmeticError, TypeError):
        return None
    return (dec / _USD_PER_MILLION).quantize(_CENTS, rounding=ROUND_HALF_UP)


def musd(value_musd: Any) -> Decimal | None:
    """Quantise an already-in-millions figure to cents (NUMERIC(14,2))."""
    if value_musd is None:
        return None
    try:
        dec = value_musd if isinstance(value_musd, Decimal) else Decimal(str(value_musd))
    except (ValueError, ArithmeticError, TypeError):
        return None
    return dec.quantize(_CENTS, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- #
# Period / year parsing                                                       #
# --------------------------------------------------------------------------- #
_YEAR_RE = re.compile(r"(19|20)\d{2}")


def year_of(period: Any) -> int | None:
    """Extract the leading 4-digit calendar year from a period string.

    Handles the period formats the connectors land: ``"2023"`` (Comtrade),
    ``"2023-05"`` (Census ``TIME``), ``"2024-03/2025-02"`` (USASpending),
    ``"FY2023"`` (filings). Returns ``None`` when no year is present.
    """
    if period is None:
        return None
    m = _YEAR_RE.search(str(period))
    return int(m.group(0)) if m else None


def period_prefix(year: int) -> str:
    """SQL ``LIKE`` pattern matching any period string containing ``year``.

    A *contains* pattern (``%2023%``) is used rather than a leading anchor
    because connectors land the period in heterogeneous shapes — ``"2023"``,
    ``"2023-05"``, ``"FY2023"``, ``"2024-03/2025-02"``. This is only the coarse
    prefilter; callers confirm the precise year with :func:`year_of` in Python.
    """
    return f"%{year}%"


# --------------------------------------------------------------------------- #
# HS-code normalisation                                                       #
# --------------------------------------------------------------------------- #
def normalize_hs(code: Any) -> str:
    """Strip dots/spaces from an HS code (``"8504.40"`` -> ``"850440"``)."""
    return re.sub(r"[.\s]", "", str(code or "")).strip()


def hs_code_set(cell: dict[str, Any]) -> set[str]:
    """Return the cell's normalised HS codes as a set (empty when none)."""
    return {normalize_hs(c) for c in (cell.get("hs_codes") or []) if normalize_hs(c)}


def hs_matches(raw_hs: Any, wanted: set[str]) -> bool:
    """True when a raw HS code maps to one of the cell's codes.

    Matching is hierarchical: a raw HS-6 code (``"850440"``) matches a cell
    HS-4 heading (``"8504"``) and vice-versa, so an import line filed at finer
    or coarser granularity than the taxonomy still lands in the right cell.
    Exact and prefix relationships in either direction are accepted.
    """
    raw = normalize_hs(raw_hs)
    if not raw or not wanted:
        return False
    for code in wanted:
        if raw == code or raw.startswith(code) or code.startswith(raw):
            return True
    return False


# --------------------------------------------------------------------------- #
# Country-code matching                                                        #
# --------------------------------------------------------------------------- #
# name -> (ISO-3, ISO-2). Covers the engagement geographies plus the common
# economies; extend as the geography config grows. Connectors land country in
# different shapes (Comtrade reporter = ISO-3, Census = "US", World Bank =
# ISO-3, PatentsView = ISO-2, USASpending = ISO-3/"USA"), so a single cell
# country must match any of its aliases.
_COUNTRY_CODES: dict[str, tuple[str, str]] = {
    "china": ("CHN", "CN"),
    "india": ("IND", "IN"),
    "japan": ("JPN", "JP"),
    "vietnam": ("VNM", "VN"),
    "south korea": ("KOR", "KR"),
    "korea, republic of": ("KOR", "KR"),
    "taiwan": ("TWN", "TW"),
    "singapore": ("SGP", "SG"),
    "malaysia": ("MYS", "MY"),
    "thailand": ("THA", "TH"),
    "indonesia": ("IDN", "ID"),
    "philippines": ("PHL", "PH"),
    "hong kong": ("HKG", "HK"),
    "united states": ("USA", "US"),
    "usa": ("USA", "US"),
    "mexico": ("MEX", "MX"),
    "brazil": ("BRA", "BR"),
    "canada": ("CAN", "CA"),
    "germany": ("DEU", "DE"),
    "france": ("FRA", "FR"),
    "united kingdom": ("GBR", "GB"),
    "italy": ("ITA", "IT"),
    "netherlands": ("NLD", "NL"),
    "belgium": ("BEL", "BE"),
    "spain": ("ESP", "ES"),
    "sweden": ("SWE", "SE"),
    "switzerland": ("CHE", "CH"),
    "austria": ("AUT", "AT"),
    "poland": ("POL", "PL"),
    "czech republic": ("CZE", "CZ"),
    "denmark": ("DNK", "DK"),
    "finland": ("FIN", "FI"),
    "norway": ("NOR", "NO"),
    "australia": ("AUS", "AU"),
    "paraguay": ("PRY", "PY"),
    "colombia": ("COL", "CO"),
    "moldova": ("MDA", "MD"),
    "georgia": ("GEO", "GE"),
}


def country_aliases(country: Any) -> set[str]:
    """Return the lower-cased match set for a cell country.

    Includes the country name itself plus its ISO-3 and ISO-2 codes (and a few
    fixed synonyms), so it can be tested against any raw country/reporter
    column regardless of which code system the connector used.
    """
    name = str(country or "").strip().lower()
    aliases: set[str] = set()
    if not name:
        return aliases
    aliases.add(name)
    codes = _COUNTRY_CODES.get(name)
    if codes:
        iso3, iso2 = codes
        aliases.update({iso3.lower(), iso2.lower()})
    if name in ("united states", "usa"):
        aliases.update({"us", "usa", "united states", "u.s.", "u.s.a."})
    return aliases


def country_matches(raw_value: Any, country: Any) -> bool:
    """True when a raw country/reporter value denotes the cell's country."""
    raw = str(raw_value or "").strip().lower()
    if not raw:
        return False
    return raw in country_aliases(country)


# --------------------------------------------------------------------------- #
# Trade-flow direction                                                         #
# --------------------------------------------------------------------------- #
_IMPORT_FLOWS = {"m", "import", "imports", "mp"}
_EXPORT_FLOWS = {"x", "export", "exports", "xp"}


def flow_is_import(raw_flow: Any) -> bool:
    return str(raw_flow or "").strip().lower() in _IMPORT_FLOWS


def flow_is_export(raw_flow: Any) -> bool:
    return str(raw_flow or "").strip().lower() in _EXPORT_FLOWS


def segment_flow_predicate(segment: Any):
    """Return a ``flow -> bool`` predicate for a geography segment.

    IMPORT cells consume import lines, EXPORT cells export lines. DOMESTIC and
    other non-cross-border segments return ``None`` (Comtrade-style trade data
    cannot size them — the method must skip the cell).
    """
    seg = str(segment or "").strip().upper()
    if seg == "IMPORT":
        return flow_is_import
    if seg == "EXPORT":
        return flow_is_export
    return None


# --------------------------------------------------------------------------- #
# Subcategory keyword extraction (for text-mining methods)                     #
# --------------------------------------------------------------------------- #
_STOPWORDS = {
    "and", "the", "for", "with", "from", "into", "other", "misc", "general",
    "integrated", "management", "systems", "system", "products", "product",
}


def subcategory_keywords(cell: dict[str, Any]) -> set[str]:
    """Tokenise a subcategory name into lower-cased content keywords.

    ``"Passives (capacitors, resistors, inductors)"`` ->
    ``{"passives", "capacitors", "resistors", "inductors"}``. Used to gate
    free-text matches in transcript/news/web-search mining so a figure only
    counts when its surrounding text is about this subcategory.
    """
    name = str(cell.get("subcategory_name") or "")
    tokens = re.split(r"[^a-zA-Z]+", name.lower())
    return {t for t in tokens if len(t) >= 4 and t not in _STOPWORDS}


def text_mentions(haystack: str, terms: Iterable[str]) -> bool:
    """True when any of ``terms`` appears (case-insensitively) in ``haystack``."""
    low = haystack.lower()
    return any(t and t in low for t in terms)


# --------------------------------------------------------------------------- #
# Currency-figure extraction (transcript / web-search mining)                  #
# --------------------------------------------------------------------------- #
_SCALE = {
    "trillion": Decimal("1e12"), "tn": Decimal("1e12"), "tr": Decimal("1e12"),
    "billion": Decimal("1e9"), "bn": Decimal("1e9"), "b": Decimal("1e9"),
    "million": Decimal("1e6"), "mn": Decimal("1e6"), "m": Decimal("1e6"),
    "thousand": Decimal("1e3"), "k": Decimal("1e3"),
}

# Require an explicit USD marker so we never lift a bare number out of prose.
# Two orderings: "$5.2 billion" and "5.2 billion dollars".
_MONEY_RE = re.compile(
    r"(?:(?:US\$|USD|\$)\s?(?P<amt1>[\d,]+(?:\.\d+)?)\s?"
    r"(?P<scale1>trillion|billion|million|thousand|tn|bn|mn|tr|[bmk])?)"
    r"|(?:(?P<amt2>[\d,]+(?:\.\d+)?)\s?(?P<scale2>trillion|billion|million|thousand)\s?"
    r"(?:US\s?)?dollars?)",
    re.IGNORECASE,
)


def extract_usd_amounts(snippet: str) -> list[Decimal]:
    """Return every USD figure in ``snippet`` as an absolute USD Decimal.

    Recognises ``$5.2 billion``, ``USD 740 million``, ``5.2 billion dollars``.
    A bare ``$5`` (no scale word) is treated as USD 5 — callers that need a
    market-scale figure should filter by magnitude. Returns ``[]`` when no
    currency-marked figure is present.
    """
    out: list[Decimal] = []
    for m in _MONEY_RE.finditer(snippet or ""):
        amt = m.group("amt1") or m.group("amt2")
        scale = (m.group("scale1") or m.group("scale2") or "").lower()
        if not amt:
            continue
        try:
            value = Decimal(amt.replace(",", ""))
        except ArithmeticError:
            continue
        if scale:
            value *= _SCALE.get(scale, Decimal(1))
        out.append(value)
    return out


def sentences(blob: str) -> list[str]:
    """Naive sentence split for context-scoped figure extraction."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", blob or "") if s.strip()]


# --------------------------------------------------------------------------- #
# Assumptions ledger lookup                                                    #
# --------------------------------------------------------------------------- #
def get_assumption(
    conn: Connection,
    *,
    subcategory_id: int | None,
    geography_id: int | None,
    year: int,
    terms: Iterable[str],
) -> dict[str, Any] | None:
    """Resolve the most specific active assumption matching ``terms``.

    Used by the proxy methods that need a conversion factor (USD per unit, ASP,
    revenue per hire …). The assumption carries its own ``source_id`` so the
    resulting estimate stays drillable. Returns ``None`` when no in-scope,
    in-effect assumption matches — the method must then skip rather than invent
    a factor.

    Specificity ordering prefers an assumption scoped to both the subcategory
    and geography, then either, then global.
    """
    term_list = [t.lower() for t in terms if t]
    if not term_list:
        return None
    like_clauses = " OR ".join(
        f"(lower(assumption_text) LIKE :t{i} OR lower(coalesce(unit,'')) "
        f"LIKE :t{i} OR lower(coalesce(derivation_method,'')) LIKE :t{i})"
        for i in range(len(term_list))
    )
    params: dict[str, Any] = {f"t{i}": f"%{t}%" for i, t in enumerate(term_list)}
    params.update({"sub": subcategory_id, "geo": geography_id, "yr": year})
    sql = text(
        f"""
        SELECT assumption_id, numeric_value, unit, source_id, assumption_text,
               (CASE WHEN scope_subcategory_id = :sub THEN 2 ELSE 0 END)
             + (CASE WHEN scope_geography_id = :geo THEN 1 ELSE 0 END) AS specificity
        FROM assumptions
        WHERE superseded_by IS NULL
          AND numeric_value IS NOT NULL
          AND source_id IS NOT NULL
          AND effective_from_year <= :yr
          AND (effective_to_year IS NULL OR effective_to_year >= :yr)
          AND (scope_subcategory_id = :sub OR scope_subcategory_id IS NULL)
          AND (scope_geography_id = :geo OR scope_geography_id IS NULL)
          AND ({like_clauses})
        ORDER BY specificity DESC, assumption_id DESC
        LIMIT 1
        """
    )
    row = conn.execute(sql, params).first()
    return dict(row._mapping) if row else None


# --------------------------------------------------------------------------- #
# Generic raw-row fetch                                                         #
# --------------------------------------------------------------------------- #
def fetch_rows(conn: Connection, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Run a read query and return a list of plain dict rows."""
    return [dict(r._mapping) for r in conn.execute(text(sql), params)]
