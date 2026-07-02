"""Seed REAL Medtech-APAC demo cells, triangulation, player shares and catalysts.

Source: Jabil MedTech APAC final report (extracted tables).
  * Family x Country TAM  -> report main.4
  * Subfamily TAM (share)  -> report subfamily tables (subfam.json)
  * Players                -> report appx.21 / main.37
  * VBP catalysts          -> report appx.20

Every TAM number is faithful to the report. Only per-method triangulation jitter
and player shares are synthesized, and they stay inside the documented confidence
bands / OEM positioning. Deterministic (no randomness) -> idempotent re-runs.

Run:
    export DATABASE_URL="postgresql+psycopg://grx10:grx10@localhost:5433/grx10_market_research"
    export CRED_MASTER_KEY="local-dev-master-key-2026"
    python scripts/seed_medtech_cells.py
"""
from __future__ import annotations

import hashlib
import os
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import create_engine, text

YEARS = (2026, 2031)
GEO = {"China": 1, "Malaysia": 2, "Singapore": 3}

# --------------------------------------------------------------------------- #
# Family x Country TAM  ($M)  — report main.4.  (2026, 2031) per country.
# --------------------------------------------------------------------------- #
FAMILY_COUNTRY_TAM: dict[str, dict[str, tuple[float, float]]] = {
    "Cardiovascular & Vascular": {
        "China":     (10963.0, 14320.0),
        "Malaysia":  (692.4,   1295.1),
        "Singapore": (2984.2,  1930.0),
    },
    "Surgical & GI Endoscopy": {
        "China":     (11947.3, 17710.7),
        "Malaysia":  (682.6,   934.2),
        "Singapore": (1878.7,  1269.0),
    },
    "In Vitro Diagnostics": {
        "China":     (12977.4, 18525.6),
        "Malaysia":  (1644.4,  2701.7),
        "Singapore": (3668.6,  4168.3),
    },
    "Patient Monitoring & Critical Care": {
        "China":     (13097.6, 16309.2),
        "Malaysia":  (713.1,   951.2),
        "Singapore": (2238.1,  1195.3),
    },
    "Medical Imaging": {
        "China":     (6966.0,  10345.2),
        "Malaysia":  (259.6,   434.5),
        "Singapore": (797.7,   1035.6),
    },
    "Consumables": {
        "China":     (15961.4, 23515.7),
        "Malaysia":  (2129.8,  5171.1),
        "Singapore": (1910.2,  1386.6),
    },
    "Orthopedics & Spine": {
        "China":     (4308.2,  3464.6),
        "Malaysia":  (225.9,   515.0),
        "Singapore": (1367.2,  1162.6),
    },
}

# --------------------------------------------------------------------------- #
# Subfamily TAM ($M) — report subfamily tables.  subcategory_id -> (2026, 2031).
# Shares within a family sum to the family total, so allocating family-country
# TAM by these shares reproduces the report exactly.
# --------------------------------------------------------------------------- #
SUBFAMILY_TAM: dict[str, list[tuple[int, float, float]]] = {
    "Cardiovascular & Vascular": [
        (11, 6531.2, 7659.6),   # Coronary Stents
        (12, 2540.8, 3324.9),   # Peripheral Vascular
        (13, 1700.3, 1849.9),   # Electrophysiology
        (14, 1711.7, 1857.5),   # Structural Heart
        (15, 2155.6, 2853.1),   # Cardiac Rhythm Management
    ],
    "Surgical & GI Endoscopy": [
        (21, 2834.9, 3899.2),   # Flexible GI Endoscopy
        (22, 2032.4, 2823.2),   # Rigid Endoscopy
        (23, 3136.1, 4263.3),   # Energy Devices
        (24, 4318.5, 5814.9),   # Endomechanical Instruments
        (25, 2186.6, 3113.3),   # Surgical Robotics
    ],
    "In Vitro Diagnostics": [
        (31, 8277.2, 11428.0),  # Chemistry & Immunoassay
        (32, 1611.0, 4317.2),   # Molecular Diagnostics
        (33, 2700.0, 3809.3),   # Point of Care Diagnostics
        (34, 4278.9, 3809.3),   # IVD Reagents & Consumables
        (35, 1423.2, 2031.6),   # Histology & Pathology
    ],
    "Patient Monitoring & Critical Care": [
        (41, 4351.8, 5091.7),   # Patient Monitors
        (42, 1431.1, 1697.2),   # Defibrillators
        (43, 5498.1, 5994.8),   # Anesthesia & Ventilators
        (44, 1469.9, 1987.4),   # Infusion Pumps
        (45, 3297.9, 3684.7),   # Dialysis
    ],
    "Medical Imaging": [
        (51, 2611.7, 3663.1),   # Computed Tomography
        (52, 1343.4, 1991.2),   # Magnetic Resonance Imaging
        (53, 1627.3, 2661.3),   # Ultrasound
        (54, 1786.5, 2550.7),   # X-Ray
        (55, 654.3,  949.0),    # Nuclear Imaging
    ],
    "Consumables": [
        (61, 8062.7, 12586.5),  # Wound Care
        (62, 4610.8, 6763.7),   # IV Sets & Syringes
        (63, 2890.7, 4195.5),   # Medical Gloves
        (64, 4437.1, 6527.6),   # Catheters
    ],
    "Orthopedics & Spine": [
        (71, 2543.5, 2170.1),   # Joint Reconstruction
        (72, 1248.3, 1058.1),   # Trauma Fixation
        (73, 1635.1, 1464.0),   # Spine
        (74, 474.5,  450.1),    # Sports Medicine
    ],
}

LARGE_FAMILIES = {
    "Cardiovascular & Vascular", "In Vitro Diagnostics",
    "Patient Monitoring & Critical Care", "Consumables", "Surgical & GI Endoscopy",
}
NICHE_SUBCATS = {25, 55, 74}  # Surgical Robotics, Nuclear Imaging, Sports Medicine

BAND = {"high": 0.10, "medium": 0.20, "low": 0.35}
JITTER = {"high": 0.04, "medium": 0.10, "low": 0.0}


def q2(x: float) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def jitter_frac(cell_id_key: str, method_code: str, max_jit: float) -> float:
    """Deterministic signed fraction in [-max_jit, +max_jit) from a hash."""
    if max_jit == 0.0:
        return 0.0
    h = int(hashlib.md5(f"{cell_id_key}:{method_code}".encode()).hexdigest(), 16)
    return ((h % 1000) / 1000.0 - 0.5) * 2.0 * max_jit


def classify(country: str, family: str, subcat_id: int, tam26: float) -> str:
    if country == "China" and family in LARGE_FAMILIES and tam26 > 300:
        return "high"
    if tam26 < 50 or (subcat_id in NICHE_SUBCATS and country in ("Malaysia", "Singapore")):
        return "low"
    return "medium"


def rationale(country: str, family: str, subcat_id: int, conf: str, tam26: float) -> str:
    if conf == "high":
        return (f"China + large family ({family}) with 2026 TAM ${tam26:,.0f}M > $300M; "
                "NMPA registrations, Comtrade flows and OEM filings converge (<10% spread).")
    if conf == "low":
        if subcat_id in NICHE_SUBCATS and country in ("Malaysia", "Singapore"):
            return (f"Niche segment in {country} with sparse market data; single-source / "
                    "proxy estimate, +/-35% band.")
        return (f"Small market (2026 TAM ${tam26:,.0f}M < $50M) in {country}; "
                "derived estimate, +/-35% band.")
    return (f"{country} mid-tier cell (2026 TAM ${tam26:,.0f}M); two independent sources "
            "with trade-data anchor, +/-20% band.")


# Triangulation recipe per confidence. Picks a family-appropriate filings source.
FILINGS_BY_FAMILY = {
    "Cardiovascular & Vascular": "microport_filings",
    "Surgical & GI Endoscopy": "mindray_filings",
    "In Vitro Diagnostics": "mindray_filings",
    "Patient Monitoring & Critical Care": "mindray_filings",
    "Medical Imaging": "united_imaging_hkex",
    "Consumables": "nipro_filings",
    "Orthopedics & Spine": "microport_filings",
}


def triangulation_plan(conf: str, family: str, key: str) -> list[tuple[str, str]]:
    """Return list of (method_code, source_id) for a cell."""
    if conf == "high":
        return [
            ("comtrade_hs4_import", "un_comtrade"),
            ("top_down_industry_allocation", "imarc"),
            ("filings_segment_extraction", FILINGS_BY_FAMILY[family]),
            ("regulatory_count_unit_price", "cn_nmpa"),
        ]
    if conf == "medium":
        # second method alternates deterministically
        h = int(hashlib.md5(key.encode()).hexdigest(), 16)
        if h % 2 == 0:
            second = ("top_down_industry_allocation", "imarc")
        else:
            second = ("activity_volume_unit_price", "who_gho")
        return [("comtrade_hs4_import", "un_comtrade"), second]
    # low: single method
    h = int(hashlib.md5(key.encode()).hexdigest(), 16)
    if h % 2 == 0:
        return [("macro_scaling", "world_bank")]
    return [("top_down_industry_allocation", "globaldata")]


# --------------------------------------------------------------------------- #
# Player shares — China 2026 producer cells.  (subcat_id, company_id, rank, share_pct)
# Shares per (cell, producer) sum <= 100; positioning per appx.21 / main.37.
# --------------------------------------------------------------------------- #
PLAYER_FILINGS = {
    1: "mindray_filings", 2: "united_imaging_hkex", 3: "globaldata",
    4: "globaldata", 5: "globaldata", 6: "microport_filings",
    7: "globaldata", 8: "globaldata", 9: "terumo_filings", 10: "nipro_filings",
}
PLAYER_SHARES = [
    # Cardiovascular
    (11, 4, 1, 23.0),  (11, 6, 2, 18.0),   # Coronary Stents: Lepu, MicroPort
    (12, 6, 1, 15.0),  (12, 4, 2, 12.0),   # Peripheral Vascular: MicroPort, Lepu
    (13, 4, 1, 14.0),  (13, 6, 2, 10.0),   # Electrophysiology: Lepu, MicroPort
    (14, 7, 1, 16.0),  (14, 8, 2, 14.0), (14, 6, 3, 10.0),  # Structural Heart: Peijia, Venus, MicroPort
    (15, 6, 1, 12.0),                       # CRM: MicroPort
    # Surgical / GI
    (21, 3, 1, 20.0),                       # Flexible GI: Micro-Tech Nanjing
    # IVD
    (31, 1, 1, 22.0),  (31, 4, 2, 8.0),     # Chemistry & Immunoassay: Mindray, Lepu
    (32, 5, 1, 18.0),                       # Molecular Dx: MGI Tech
    (34, 1, 1, 18.0),                       # IVD Reagents: Mindray
    # Patient Monitoring
    (41, 1, 1, 28.0),                       # Patient Monitors: Mindray
    (42, 1, 1, 20.0),                       # Defibrillators: Mindray
    (45, 10, 1, 14.0),                      # Dialysis: Nipro
    # Imaging
    (51, 2, 1, 25.0),                       # CT: United Imaging
    (52, 2, 1, 22.0),                       # MRI: United Imaging
    (53, 1, 1, 24.0),                       # Ultrasound: Mindray
    (54, 2, 1, 20.0),                       # X-Ray: United Imaging
    (55, 2, 1, 30.0),                       # Nuclear Imaging: United Imaging
    # Consumables
    (62, 10, 1, 12.0),                      # IV Sets & Syringes: Nipro
    (64, 9, 1, 15.0),  (64, 10, 2, 8.0),    # Catheters: Terumo, Nipro
    # Orthopedics
    (71, 6, 1, 14.0),                       # Joint Reconstruction: MicroPort
    (73, 6, 1, 10.0),                       # Spine: MicroPort
]

# --------------------------------------------------------------------------- #
# VBP catalysts — report appx.20.  (subcat_id, company_id|None, quarter, desc)
# Linked to China cells; impact_direction 'negative' (price-cut event).
# --------------------------------------------------------------------------- #
CATALYSTS = [
    (11, 4,    "2020-Q4",
     "National coronary drug-eluting stent VBP (Nov 2020): CNY 13,000 -> CNY 700-800 "
     "(93-94% cut). Implant volumes accelerated as access expanded to lower-tier hospitals."),
    (73, None, "2021-Q3",
     "Spinal implant VBP national pilot (2021): 60-80% price cut across categories. "
     "MNC orthopaedic firms accelerated China localisation; domestic champions gained volume."),
    (71, None, "2022-Q1",
     "Artificial joints VBP Round 4 (2021-2022): knee CNY 32,000 -> CNY 5,329 (82-84% cut). "
     "Zimmer/Stryker/DePuy restructured supply chains; AK Medical and WeGo gained share."),
    (34, 1,    "2023-Q2",
     "IVD reagents 23-province alliance VBP (2023): 40-60% cut on chemistry/immunoassay panels. "
     "Domestic IVD firms (Mindray, Autobio, Snibe) absorbed cuts; foreign reagent revenue fell."),
    (45, 10,   "2024-Q1",
     "Blood purification (dialysis) VBP (2024): 40-70% cut on membranes/dialyzers. "
     "With ~2M ESRD patients on 3x weekly dialysis, volume growth partly offset margin compression."),
    # Intraocular Lenses (Round 5, 2024) omitted: no ophthalmology subcategory in this taxonomy.
]


def main() -> None:
    url = os.environ["DATABASE_URL"]
    engine = create_engine(url, future=True)

    n_cells = n_tri = n_shares = n_cat = 0
    with engine.begin() as conn:
        # 1) Cells + triangulation -------------------------------------------------
        for family, subs in SUBFAMILY_TAM.items():
            tot26 = sum(s[1] for s in subs)
            tot31 = sum(s[2] for s in subs)
            for subcat_id, sub26, sub31 in subs:
                share26 = sub26 / tot26
                share31 = sub31 / tot31
                for country, geo_id in GEO.items():
                    ctam26, ctam31 = FAMILY_COUNTRY_TAM[family][country]
                    tam26 = ctam26 * share26
                    conf = classify(country, family, subcat_id, tam26)
                    band = BAND[conf]
                    why = rationale(country, family, subcat_id, conf, tam26)
                    for year in YEARS:
                        tam = (ctam26 * share26) if year == 2026 else (ctam31 * share31)
                        low = tam * (1 - band)
                        high = tam * (1 + band)
                        cid = conn.execute(
                            text(
                                "INSERT INTO cells (subcategory_id, geography_id, year, "
                                " tam_revenue_usd_m, tam_low_usd_m, tam_high_usd_m, "
                                " confidence, confidence_rationale, status) "
                                "VALUES (:sc, :geo, :yr, :tam, :low, :high, :conf, :why, 'active') "
                                "ON CONFLICT (subcategory_id, geography_id, year) DO UPDATE SET "
                                " tam_revenue_usd_m = EXCLUDED.tam_revenue_usd_m, "
                                " tam_low_usd_m = EXCLUDED.tam_low_usd_m, "
                                " tam_high_usd_m = EXCLUDED.tam_high_usd_m, "
                                " confidence = EXCLUDED.confidence, "
                                " confidence_rationale = EXCLUDED.confidence_rationale, "
                                " updated_at = now() "
                                "RETURNING cell_id"
                            ),
                            {"sc": subcat_id, "geo": geo_id, "yr": year,
                             "tam": q2(tam), "low": q2(low), "high": q2(high),
                             "conf": conf, "why": why},
                        ).scalar()
                        n_cells += 1

                        key = f"{subcat_id}-{geo_id}-{year}"
                        for method_code, source_id in triangulation_plan(conf, family, key):
                            est = tam * (1 + jitter_frac(key, method_code, JITTER[conf]))
                            est = max(low, min(high, est))
                            conn.execute(
                                text(
                                    "INSERT INTO cell_triangulation "
                                    " (cell_id, method_code, estimate_usd_m, source_id, notes) "
                                    "VALUES (:cid, :mc, :est, :src, :notes) "
                                    "ON CONFLICT (cell_id, method_code, source_id) DO UPDATE SET "
                                    " estimate_usd_m = EXCLUDED.estimate_usd_m, notes = EXCLUDED.notes"
                                ),
                                {"cid": cid, "mc": method_code, "est": q2(est),
                                 "src": source_id,
                                 "notes": f"{method_code} via {source_id} ({conf} cell)"},
                            )
                            n_tri += 1

        # 2) Player shares — China 2026 producer cells ----------------------------
        for subcat_id, company_id, rank, share_pct in PLAYER_SHARES:
            row = conn.execute(
                text("SELECT cell_id, tam_revenue_usd_m, confidence FROM cells "
                     "WHERE subcategory_id = :sc AND geography_id = 1 AND year = 2026"),
                {"sc": subcat_id},
            ).fetchone()
            if row is None:
                continue
            cid, tam, conf = row.cell_id, float(row.tam_revenue_usd_m), row.confidence
            src = PLAYER_FILINGS[company_id]
            conn.execute(
                text(
                    "INSERT INTO player_shares (cell_id, company_id, player_role, rank, "
                    " share_pct, share_low_pct, share_high_pct, revenue_usd_m, source_id, confidence) "
                    "VALUES (:cid, :co, 'producer', :rank, :sp, :slo, :shi, :rev, :src, :conf) "
                    "ON CONFLICT (cell_id, company_id, player_role) DO UPDATE SET "
                    " rank = EXCLUDED.rank, share_pct = EXCLUDED.share_pct, "
                    " share_low_pct = EXCLUDED.share_low_pct, share_high_pct = EXCLUDED.share_high_pct, "
                    " revenue_usd_m = EXCLUDED.revenue_usd_m, source_id = EXCLUDED.source_id, "
                    " confidence = EXCLUDED.confidence"
                ),
                {"cid": cid, "co": company_id, "rank": rank,
                 "sp": q2(share_pct), "slo": q2(max(0.0, share_pct - 2.0)),
                 "shi": q2(share_pct + 2.0), "rev": q2(tam * share_pct / 100.0),
                 "src": src, "conf": conf},
            )
            n_shares += 1

        # 3) Catalysts — VBP rounds (idempotent: clear VBP-sourced rows first) -----
        conn.execute(text("DELETE FROM catalysts WHERE source_id = 'cn_nhsa_vbp'"))
        for subcat_id, company_id, quarter, desc in CATALYSTS:
            cid = conn.execute(
                text("SELECT cell_id FROM cells WHERE subcategory_id = :sc "
                     "AND geography_id = 1 AND year = 2026"),
                {"sc": subcat_id},
            ).scalar()
            if cid is None:
                continue
            conn.execute(
                text(
                    "INSERT INTO catalysts (cell_id, company_id, catalyst_type, "
                    " impact_direction, expected_quarter, description, source_id) "
                    "VALUES (:cid, :co, 'volume_based_procurement', 'negative', :q, :d, 'cn_nhsa_vbp')"
                ),
                {"cid": cid, "co": company_id, "q": quarter, "d": desc},
            )
            n_cat += 1

        # 4) Refresh the confidence summary materialized view ----------------------
        conn.execute(text("REFRESH MATERIALIZED VIEW cell_triangulation_summary"))

    print(f"cells upserted:          {n_cells}")
    print(f"triangulation rows:      {n_tri}")
    print(f"player_shares upserted:  {n_shares}")
    print(f"catalysts inserted:      {n_cat}")


if __name__ == "__main__":
    main()
