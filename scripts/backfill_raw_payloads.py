"""Backfill raw_* payload rows for the Medtech-APAC demo dataset.

The demo loader created ``cell_triangulation`` estimates but never wrote the
raw evidence rows they drill into, so Cell Detail's two-click audit chain
dead-ended at an empty payload. This script writes one source-shaped raw row
per evidence unit, matching how each connector's ``normalize()`` would have
landed the data:

  un_comtrade        -> raw_trade_flows       one row per (reporter, HS4) at the
                                              latest actual reference year (2024);
                                              projections drill to actuals.
  imarc / globaldata -> raw_industry_reports  one report extract per
                                              (subcategory, country, year).
  who_gho            -> raw_external_metrics  one GHO indicator fact per country.
  world_bank         -> raw_external_metrics  WDI facts per country.
  cn_nmpa            -> raw_regulatory        one registration-count snapshot per
                                              subcategory (China).
  *_filings          -> raw_filings           one segment-revenue extract per
                                              (filer, segment, geography).

Typed columns (reporter/period/market/segment/geography/...) are filled so the
cell-aware raw_ref resolver in ``routers/cells.py`` can match a cell to its own
evidence row instead of "latest row per source".

Idempotent: refuses to run if any target table already has rows, unless
``--force`` is passed (which deletes only rows for the sources written here).

Run:  python scripts/backfill_raw_payloads.py            (needs DATABASE_URL)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://grx10:grx10@localhost:5433/grx10_market_research",
)

# Latest actual reference periods — estimates for 2026/2031 cells are derived
# from these actuals plus the growth model, so projections drill to actuals.
TRADE_REF_YEAR = 2024
GHO_REF_YEAR = 2022
WDI_REF_YEAR = 2024
FILING_PERIOD = "FY2025"

REPORTER_CODES = {"China": 156, "Malaysia": 458, "Singapore": 702}
ISO3 = {"China": "CHN", "Malaysia": "MYS", "Singapore": "SGP"}

HS_DESCRIPTIONS = {
    "9018": "Instruments and appliances used in medical, surgical, dental or veterinary sciences",
    "9019": "Mechano-therapy appliances; therapy and artificial respiration apparatus",
    "9021": "Orthopaedic appliances; artificial parts of the body; hearing aids; pacemakers",
    "9022": "Apparatus based on the use of X-rays or of alpha, beta or gamma radiations",
    "3822": "Diagnostic or laboratory reagents; certified reference materials",
    "3005": "Wadding, gauze, bandages and similar articles, medical-coated or retail-packed",
    "4015": "Articles of apparel and clothing accessories of vulcanised rubber (incl. gloves)",
}

# Share of apparent market met by imports — used to derive a plausible import
# value from the demand-side estimate (domestic production covers the rest).
IMPORT_SHARE = {"China": 0.52, "Malaysia": 0.83, "Singapore": 0.91}

# WHO GHED current health expenditure per capita (US$), latest actuals.
GHO_CHE_PC = {"China": 671.8, "Malaysia": 487.3, "Singapore": 3969.4}

# World Bank WDI actuals.
WDI = {
    "China": {"SP.POP.TOTL": 1_408_975_000, "NY.GDP.PCAP.CD": 13_303.1},
    "Malaysia": {"SP.POP.TOTL": 34_308_525, "NY.GDP.PCAP.CD": 12_570.5},
    "Singapore": {"SP.POP.TOTL": 6_036_860, "NY.GDP.PCAP.CD": 90_674.0},
}
WDI_NAMES = {"SP.POP.TOTL": "Population, total", "NY.GDP.PCAP.CD": "GDP per capita (current US$)"}

# NMPA classification catalogue code per taxonomy family.
NMPA_FAMILY_CODES = {
    "Cardiovascular & Vascular": ("6877", "Interventional devices (介入器材)"),
    "Surgical & GI Endoscopy": ("6822", "Medical optical instruments and endoscopic equipment"),
    "In Vitro Diagnostics": ("6840", "Clinical laboratory analytical instruments and diagnostic reagents"),
    "Patient Monitoring & Critical Care": ("6821", "Medical electronic instruments and equipment"),
    "Medical Imaging": ("6830", "Medical X-ray and imaging equipment"),
    "Consumables": ("6866", "Medical polymer materials and products"),
    "Orthopedics & Spine": ("6846", "Implantable materials and artificial organs"),
}
NMPA_SAMPLE_HOLDERS = {
    "Cardiovascular & Vascular": ["Lepu Medical Technology (Beijing) Co., Ltd.", "MicroPort Scientific Corporation", "Venus Medtech (Hangzhou) Inc."],
    "Surgical & GI Endoscopy": ["Micro-Tech (Nanjing) Co., Ltd.", "Sonoscape Medical Corp.", "Aohua Endoscopy Co., Ltd."],
    "In Vitro Diagnostics": ["Shenzhen Mindray Bio-Medical Electronics Co., Ltd.", "MGI Tech Co., Ltd.", "Maccura Biotechnology Co., Ltd."],
    "Patient Monitoring & Critical Care": ["Shenzhen Mindray Bio-Medical Electronics Co., Ltd.", "Comen Medical Instruments Co., Ltd.", "Aeonmed Co., Ltd."],
    "Medical Imaging": ["Shanghai United Imaging Healthcare Co., Ltd.", "Neusoft Medical Systems Co., Ltd.", "Wandong Medical Technology Co., Ltd."],
    "Consumables": ["Weigao Group Medical Polymer Co., Ltd.", "Kangdelai Medical Devices Co., Ltd.", "Intco Medical Technology Co., Ltd."],
    "Orthopedics & Spine": ["MicroPort Scientific Corporation", "Weigao Orthopaedic Device Co., Ltd.", "AK Medical Holdings Limited"],
}

FILERS = {
    "mindray_filings": {
        "filer": "Shenzhen Mindray Bio-Medical Electronics Co., Ltd.",
        "ticker": "300760.SZ",
        "document": "2025 Annual Report — segment and regional revenue disclosure",
        "currency": "CNY",
        "fx_to_usd": 7.11,
        "doc_url": "https://www.mindray.com/en/investor/annual-reports/2025",
        "default_share": 0.18,
    },
    "microport_filings": {
        "filer": "MicroPort Scientific Corporation",
        "ticker": "0853.HK",
        "document": "2025 Annual Report — segment revenue by business line and region",
        "currency": "USD",
        "fx_to_usd": 1.0,
        "doc_url": "https://www.microport.com/investor/annual-reports/2025",
        "default_share": 0.12,
    },
    "nipro_filings": {
        "filer": "Nipro Corporation",
        "ticker": "8086.T",
        "document": "FY2025 Securities Report (Yukashoken Hokokusho) — segment information",
        "currency": "JPY",
        "fx_to_usd": 154.8,
        "doc_url": "https://www.nipro.co.jp/en/ir/library/securities_report/2025",
        "default_share": 0.10,
    },
}

TARGET_TABLES = [
    "raw_trade_flows",
    "raw_industry_reports",
    "raw_external_metrics",
    "raw_regulatory",
    "raw_filings",
]

BACKFILL_SOURCES = (
    "un_comtrade", "imarc", "globaldata", "who_gho", "world_bank",
    "cn_nmpa", "mindray_filings", "microport_filings", "nipro_filings",
)


def _stable_frac(key: str) -> float:
    """Deterministic pseudo-random fraction in [0, 1) from a string key."""
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def _accessed_at(key: str) -> datetime:
    """Deterministic ingest timestamp during the June 2026 refresh window."""
    base = datetime(2026, 6, 18, 6, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(minutes=int(_stable_frac(key) * 60 * 24 * 9))


def _f(x) -> float:
    return float(x) if isinstance(x, Decimal) else x


def load_context(conn):
    """Triangulation rows joined with cell / subcategory / geography context."""
    rows = conn.execute(text("""
        SELECT ct.triangulation_id, ct.cell_id, ct.method_code, ct.source_id,
               ct.estimate_usd_m, c.year, sc.subcategory_id, sc.name AS subcat,
               sc.hs_codes, f.name AS family, g.country
        FROM cell_triangulation ct
        JOIN cells c            ON c.cell_id = ct.cell_id
        JOIN taxonomy_subcategories sc ON sc.subcategory_id = c.subcategory_id
        JOIN taxonomy_families f       ON f.family_id = sc.family_id
        JOIN geographies g             ON g.geography_id = c.geography_id
        ORDER BY ct.triangulation_id
    """)).mappings().all()
    return [dict(r) for r in rows]


# ─── builders ─────────────────────────────────────────────────────────────────

def build_trade_flows(tris):
    """One Comtrade-shaped row per (reporter country, HS4) at the latest actual year."""
    alloc: dict[tuple[str, str], float] = defaultdict(float)
    for t in tris:
        if t["source_id"] != "un_comtrade" or t["year"] != 2026:
            continue
        codes = t["hs_codes"] or ["9018"]
        per_code = _f(t["estimate_usd_m"]) / len(codes)
        for code in codes:
            alloc[(t["country"], code)] += per_code

    rows = []
    for (country, code), musd in sorted(alloc.items()):
        import_value = round(musd * 1e6 * IMPORT_SHARE[country])
        key = f"comtrade|{country}|{code}"
        raw_json = {
            "typeCode": "C", "freqCode": "A", "refPeriodId": TRADE_REF_YEAR,
            "refYear": TRADE_REF_YEAR, "period": str(TRADE_REF_YEAR),
            "reporterCode": REPORTER_CODES[country], "reporterISO": ISO3[country],
            "reporterDesc": country,
            "flowCode": "M", "flowDesc": "Import",
            "partnerCode": 0, "partnerISO": "W00", "partnerDesc": "World",
            "classificationCode": "HS", "isOriginalClassification": True,
            "cmdCode": code,
            "cmdDesc": HS_DESCRIPTIONS.get(code, "Medical and surgical instruments and appliances"),
            "aggrLevel": 4, "customsCode": "C00", "mosCode": "0",
            "cifvalue": round(import_value * 1.012),
            "fobvalue": None,
            "primaryValue": import_value,
            "netWgt": None, "qty": None, "qtyUnitAbbr": "N/A",
            "isReported": True, "isAggregate": False,
        }
        rows.append({
            "source_id": "un_comtrade", "accessed_at": _accessed_at(key),
            "raw_json": json.dumps(raw_json),
            "reporter": country, "partner": "World", "hs_code": code,
            "hs_version": "HS", "flow": "M", "period": str(TRADE_REF_YEAR),
            "value_usd": import_value, "qty": None, "qty_unit": None,
        })
    return rows


def build_industry_reports(tris):
    """One report extract per (source, subcategory, country, year)."""
    seen, rows = set(), []
    titles = {
        "imarc": "APAC Medical Devices Market Report 2026-2031: {family}",
        "globaldata": "MedTech Market Model Q2 2026 — {family}, Asia-Pacific",
    }
    urls = {
        "imarc": "https://www.imarcgroup.com/report/apac-medical-devices/{slug}",
        "globaldata": "https://www.globaldata.com/store/report/medtech-{slug}-apac",
    }
    publishers = {"imarc": "IMARC Group", "globaldata": "GlobalData"}
    for t in tris:
        if t["source_id"] not in ("imarc", "globaldata"):
            continue
        market = f"{t['subcat']} - {t['country']}"
        dedupe = (t["source_id"], market, t["year"])
        if dedupe in seen:
            continue
        seen.add(dedupe)
        slug = t["family"].lower().replace(" & ", "-").replace(" ", "-")
        musd = _f(t["estimate_usd_m"])
        cagr = round(4.5 + _stable_frac(f"cagr|{market}") * 6.5, 1)
        key = f"report|{t['source_id']}|{market}|{t['year']}"
        raw_json = {
            "publisher": publishers[t["source_id"]],
            "report_title": titles[t["source_id"]].format(family=t["family"]),
            "report_edition": "June 2026",
            "table_reference": f"Table {30 + t['subcategory_id'] % 40}: Market size by segment and country",
            "extract": {
                "segment": t["subcat"],
                "family": t["family"],
                "country": t["country"],
                "year": t["year"],
                "market_size_usd_m": round(musd, 1),
                "cagr_2026_2031_pct": cagr,
                "scope_note": "Manufacturer revenue, ex-factory prices, excludes aftermarket services.",
            },
            "doc_url": urls[t["source_id"]].format(slug=slug),
        }
        rows.append({
            "table": "raw_industry_reports",
            "source_id": t["source_id"], "accessed_at": _accessed_at(key),
            "raw_json": json.dumps(raw_json),
            "publisher": publishers[t["source_id"]], "market": market,
            "period": str(t["year"]), "tam_usd": round(musd * 1e6),
            "doc_url": raw_json["doc_url"],
        })
    return rows


def build_external_metrics(tris):
    """Country-level indicator facts: WHO GHED expenditure + World Bank WDI."""
    rows = []
    gho_countries = sorted({t["country"] for t in tris if t["source_id"] == "who_gho"})
    for country in gho_countries:
        key = f"gho|{country}"
        value = GHO_CHE_PC[country]
        raw_json = {
            "Id": 27000000 + REPORTER_CODES[country],
            "IndicatorCode": "GHED_CHE_pc_US_SHA2011",
            "IndicatorName": "Current health expenditure (CHE) per capita in US$",
            "SpatialDimType": "COUNTRY", "SpatialDim": ISO3[country],
            "TimeDimType": "YEAR", "TimeDim": GHO_REF_YEAR,
            "Dim1Type": None, "Dim1": None,
            "NumericValue": value, "Low": None, "High": None,
            "Value": f"{value:,.1f}",
            "Date": f"{GHO_REF_YEAR + 2}-02-14T08:00:00+00:00",
        }
        rows.append({
            "source_id": "who_gho", "accessed_at": _accessed_at(key),
            "raw_json": json.dumps(raw_json),
            "indicator": "GHED_CHE_pc_US_SHA2011", "country": country,
            "period": str(GHO_REF_YEAR), "value": value, "unit": "USD per capita",
        })

    wb_countries = sorted({t["country"] for t in tris if t["source_id"] == "world_bank"})
    for country in wb_countries:
        for code, value in WDI[country].items():
            key = f"wdi|{country}|{code}"
            raw_json = {
                "indicator": {"id": code, "value": WDI_NAMES[code]},
                "country": {"id": ISO3[country][:2], "value": country},
                "countryiso3code": ISO3[country],
                "date": str(WDI_REF_YEAR), "value": value,
                "unit": "", "obs_status": "", "decimal": 1,
            }
            rows.append({
                "source_id": "world_bank", "accessed_at": _accessed_at(key),
                "raw_json": json.dumps(raw_json),
                "indicator": code, "country": country,
                "period": str(WDI_REF_YEAR), "value": value,
                "unit": WDI_NAMES[code],
            })
    return rows


def build_regulatory(tris):
    """One NMPA registration-count snapshot per subcategory (China)."""
    seen, rows = set(), []
    for t in tris:
        if t["source_id"] != "cn_nmpa" or t["subcat"] in seen:
            continue
        seen.add(t["subcat"])
        code, code_desc = NMPA_FAMILY_CODES[t["family"]]
        holders = NMPA_SAMPLE_HOLDERS[t["family"]]
        n_reg = 240 + int(_stable_frac(f"nmpa|{t['subcat']}") * 900)
        base_2026 = next(
            (_f(x["estimate_usd_m"]) for x in tris
             if x["source_id"] == "cn_nmpa" and x["subcat"] == t["subcat"] and x["year"] == 2026),
            _f(t["estimate_usd_m"]),
        )
        rev_per_reg = round(base_2026 * 1e6 / n_reg)
        key = f"nmpa|{t['subcat']}"
        samples = [
            {
                "registration_no": f"国械注准2024{3 + i}{20000 + (t['subcategory_id'] * 37 + i * 911) % 70000}",
                "holder": holders[i % len(holders)],
                "product_name": f"{t['subcat']} device — model series {chr(65 + i)}",
                "status": "active",
                "valid_until": f"202{7 + i}-12-31",
            }
            for i in range(3)
        ]
        raw_json = {
            "registry": "NMPA Domestic Medical Device Registration Database",
            "query": {
                "product_category": t["subcat"],
                "nmpa_class_code": code,
                "nmpa_class_desc": code_desc,
                "status": "active",
                "as_of": "2026-05-31",
            },
            "result": {
                "active_registrations": n_reg,
                "new_registrations_trailing_12m": int(n_reg * 0.14),
                "implied_revenue_per_registration_usd": rev_per_reg,
            },
            "sample_registrations": samples,
        }
        rows.append({
            "source_id": "cn_nmpa", "accessed_at": _accessed_at(key),
            "raw_json": json.dumps(raw_json, ensure_ascii=False),
            "registration_id": f"NMPA-{code}-2026-05",
            "holder": holders[0], "product_code": code,
            "country": "China", "status": "active",
        })
    return rows


def build_filings(tris, conn):
    """One segment-revenue extract per (filer source, segment, geography)."""
    shares = {
        (r[0], r[1]): _f(r[2]) / 100.0
        for r in conn.execute(text("""
            SELECT ps.cell_id, ps.source_id, ps.share_pct
            FROM player_shares ps WHERE ps.share_pct IS NOT NULL
        """))
    }
    seen, rows = set(), []
    for t in tris:
        meta = FILERS.get(t["source_id"])
        if meta is None:
            continue
        dedupe = (t["source_id"], t["subcat"], t["country"])
        if dedupe in seen:
            continue
        seen.add(dedupe)
        base_2026 = next(
            (_f(x["estimate_usd_m"]) for x in tris
             if x["source_id"] == t["source_id"] and x["subcat"] == t["subcat"]
             and x["country"] == t["country"] and x["year"] == 2026),
            _f(t["estimate_usd_m"]),
        )
        share = shares.get((t["cell_id"], t["source_id"]), meta["default_share"])
        revenue_usd = round(base_2026 * 1e6 * share)
        reported = round(revenue_usd * meta["fx_to_usd"] / 1e6, 1)
        key = f"filing|{t['source_id']}|{t['subcat']}|{t['country']}"
        raw_json = {
            "filer": meta["filer"], "ticker": meta["ticker"],
            "document": meta["document"], "fiscal_period": FILING_PERIOD,
            "section": "Segment information — revenue by product line and region",
            "extract": {
                "segment": t["subcat"],
                "region": t["country"],
                "revenue_reported_m": reported,
                "reporting_currency": meta["currency"],
                "fx_rate_to_usd": meta["fx_to_usd"],
                "revenue_usd_m": round(revenue_usd / 1e6, 1),
            },
            "extraction_note": (
                "Regional product-line revenue extracted from audited segment "
                "disclosure; market size derived by dividing by the filer's "
                "estimated segment share."
            ),
            "doc_url": meta["doc_url"],
        }
        rows.append({
            "source_id": t["source_id"], "accessed_at": _accessed_at(key),
            "raw_json": json.dumps(raw_json),
            "filer": meta["filer"], "ticker": meta["ticker"],
            "period": FILING_PERIOD, "segment": t["subcat"],
            "geography": t["country"], "revenue_usd": revenue_usd,
            "doc_url": meta["doc_url"],
        })
    return rows


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="Delete previously backfilled rows for these sources first.")
    args = parser.parse_args()

    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        occupied = {
            tbl: conn.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar_one()
            for tbl in TARGET_TABLES
        }
        if any(occupied.values()) and not args.force:
            print(f"Target tables not empty ({occupied}); rerun with --force "
                  f"to replace rows for sources {BACKFILL_SOURCES}.")
            return 1
        if args.force:
            for tbl in TARGET_TABLES:
                conn.execute(
                    text(f"DELETE FROM {tbl} WHERE source_id = ANY(:src)"),
                    {"src": list(BACKFILL_SOURCES)},
                )

        tris = load_context(conn)
        print(f"{len(tris)} triangulation rows loaded.")

        inserts = {
            "raw_trade_flows": (
                build_trade_flows(tris),
                """INSERT INTO raw_trade_flows
                   (source_id, accessed_at, raw_json, reporter, partner, hs_code,
                    hs_version, flow, period, value_usd, qty, qty_unit)
                   VALUES (:source_id, :accessed_at, CAST(:raw_json AS jsonb), :reporter,
                           :partner, :hs_code, :hs_version, :flow, :period,
                           :value_usd, :qty, :qty_unit)""",
            ),
            "raw_industry_reports": (
                build_industry_reports(tris),
                """INSERT INTO raw_industry_reports
                   (source_id, accessed_at, raw_json, publisher, market, period,
                    tam_usd, doc_url)
                   VALUES (:source_id, :accessed_at, CAST(:raw_json AS jsonb), :publisher,
                           :market, :period, :tam_usd, :doc_url)""",
            ),
            "raw_external_metrics": (
                build_external_metrics(tris),
                """INSERT INTO raw_external_metrics
                   (source_id, accessed_at, raw_json, indicator, country, period,
                    value, unit)
                   VALUES (:source_id, :accessed_at, CAST(:raw_json AS jsonb), :indicator,
                           :country, :period, :value, :unit)""",
            ),
            "raw_regulatory": (
                build_regulatory(tris),
                """INSERT INTO raw_regulatory
                   (source_id, accessed_at, raw_json, registration_id, holder,
                    product_code, country, status)
                   VALUES (:source_id, :accessed_at, CAST(:raw_json AS jsonb),
                           :registration_id, :holder, :product_code, :country, :status)""",
            ),
            "raw_filings": (
                build_filings(tris, conn),
                """INSERT INTO raw_filings
                   (source_id, accessed_at, raw_json, filer, ticker, period,
                    segment, geography, revenue_usd, doc_url)
                   VALUES (:source_id, :accessed_at, CAST(:raw_json AS jsonb), :filer,
                           :ticker, :period, :segment, :geography, :revenue_usd,
                           :doc_url)""",
            ),
        }

        for table, (rows, stmt) in inserts.items():
            clean = [{k: v for k, v in r.items() if k != "table"} for r in rows]
            if clean:
                conn.execute(text(stmt), clean)
            print(f"{table}: {len(clean)} rows inserted.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
