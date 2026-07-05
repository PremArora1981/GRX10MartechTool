"""Connector admin API — ``/connectors/*``.

Every endpoint in this router manages the self-service connector layer described
in v1-definition Q6/Q7/Q9. The surface covers:

* **GET  /connectors/catalog** — static catalog (from ``docs/connector-catalog.md``
  ground truth) merged with live ``sources`` table state.
* **GET  /connectors/health** — last_probe_* columns + budget/quota warnings for
  every enabled source.
* **POST /connectors** — select an existing catalog connector (upsert a ``sources``
  row from the static entry) or add a new one by providing a full row body.
* **POST /connectors/custom** — create a user-defined declarative generic-REST
  source with an optional field map.
* **POST /connectors/{source_id}/credential** — admin-gated write-only credential
  entry. Envelope-encrypts via pgcrypto and never returns the secret (Q9).
* **DELETE /connectors/{source_id}/credential** — admin-gated credential revocation.
* **POST /connectors/{source_id}/probe** — run the source's connector probe now;
  writes back ``last_probe_*`` and returns the 7-state result.
* **POST /connectors/{source_id}/suggest-mapping** — AI-assisted field-mapping
  stub; calls the Anthropic API when ``ANTHROPIC_API_KEY`` is set.

Role model (Q10):
    ``analyst`` and above can view catalog + health.
    ``owner``/``admin`` required for credential write/revoke and source creation.
    Probe is available to ``analyst`` and above.
"""

from __future__ import annotations

import json
import logging
import textwrap
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.deps import DbSession, CurrentUserDep, EngagementDep, require_admin
from backend.app.schemas import SourceOut
from backend.app.services import credential_service
from backend.app.services.credential_service import CredentialServiceError
from connectors.base import ProbeResult, classify_http_error, classify_exception
from connectors.registry import discover, get_connector

logger = logging.getLogger("grx10.routers.connectors")

router = APIRouter(prefix="/connectors", tags=["connectors"])

# =========================================================================== #
# Static catalog — embedded from docs/connector-catalog.md (ground truth).
# Each entry carries the fields needed to seed a ``sources`` row + the static
# metadata (wave, feasibility, cost_type) that the frontend catalog screen needs.
# =========================================================================== #

_CATALOG: list[dict[str, Any]] = [
    # ---- Trade & customs → raw_trade_flows -----------------------------------
    {
        "source_id": "un_comtrade", "publisher": "UN Comtrade", "source_class": "A",
        "raw_table": "raw_trade_flows", "auth": "api_key", "access_method": "api",
        "connector": "comtrade", "wave": 1, "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://comtradeapi.un.org/data/v1/",
        "notes": "~500 calls/day free quota. HS-code versioning H0–H6.",
    },
    {
        "source_id": "us_census_intltrade", "publisher": "US Census Bureau — International Trade",
        "source_class": "A", "raw_table": "raw_trade_flows", "auth": "api_key",
        "access_method": "api", "connector": "census_intltrade", "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://api.census.gov/data/timeseries/intltrade/",
        "notes": "US-only, authoritative. Array-of-arrays JSON.",
    },
    {
        "source_id": "eurostat_comext", "publisher": "Eurostat Comext",
        "source_class": "A", "raw_table": "raw_trade_flows", "auth": "none",
        "access_method": "api", "connector": "eurostat_comext", "wave": 2,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/",
        "notes": "CN8 codes; bulk monthly CSV. Use async above threshold.",
    },
    {
        "source_id": "wto_timeseries", "publisher": "WTO Time Series",
        "source_class": "A", "raw_table": "raw_trade_flows", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://api.wto.org/timeseries/v1/",
        "notes": "Aggregates + tariffs. Weaker at HS6 line detail.",
    },
    {
        "source_id": "aseanstats", "publisher": "ASEANStats",
        "source_class": "A", "raw_table": "raw_trade_flows", "auth": "none",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://data.aseanstats.org/",
        "notes": "AHTN codes; reverse-engineer indicator codes from portal XHR.",
    },
    {
        "source_id": "usitc_dataweb", "publisher": "USITC DataWeb",
        "source_class": "A", "raw_table": "raw_trade_flows", "auth": "login",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://dataweb.usitc.gov/",
        "notes": "US tariff depth; complex query JSON.",
    },
    {
        "source_id": "china_gacc", "publisher": "China GACC",
        "source_class": "B", "raw_table": "raw_trade_flows", "auth": "none",
        "access_method": "scrape", "connector": None, "wave": 3,
        "feasibility": "HARD", "cost_type": "free",
        "url_pattern": "https://chinadata.live/",
        "notes": "No official API. Use chinadata.live as Class-B intermediary.",
    },
    {
        "source_id": "india_tradestat", "publisher": "India TRADESTAT",
        "source_class": "B", "raw_table": "raw_trade_flows", "auth": "none",
        "access_method": "scrape", "connector": None, "wave": 3,
        "feasibility": "HARD", "cost_type": "free",
        "url_pattern": "https://tradestat.commerce.gov.in/",
        "notes": "Prefer Comtrade for India. data.gov.in subset is EASY but limited.",
    },
    # ---- Regulatory → raw_regulatory -----------------------------------------
    {
        "source_id": "openfda", "publisher": "openFDA",
        "source_class": "A", "raw_table": "raw_regulatory", "auth": "api_key",
        "access_method": "api", "connector": "openfda", "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://api.fda.gov/",
        "notes": "240/min, 120k/day keyed. Bulk = full re-dumps.",
    },
    {
        "source_id": "fda_accessgudid", "publisher": "FDA AccessGUDID",
        "source_class": "A", "raw_table": "raw_regulatory", "auth": "none",
        "access_method": "api", "connector": "fda_accessgudid", "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://accessgudid.nlm.nih.gov/api/2/",
        "notes": "Weekly delta ZIP ~1MB; full ~511MB/5.1M records.",
    },
    {
        "source_id": "eu_eudamed", "publisher": "EU EUDAMED",
        "source_class": "A", "raw_table": "raw_regulatory", "auth": "none",
        "access_method": "api", "connector": None, "wave": 3,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://ec.europa.eu/tools/eudamed/",
        "notes": "Undocumented REST backend; fragile, no SLA — build breakage monitor.",
    },
    {
        "source_id": "cn_nmpa", "publisher": "China NMPA",
        "source_class": "A", "raw_table": "raw_regulatory", "auth": "none",
        "access_method": "scrape", "connector": None, "wave": 3,
        "feasibility": "HARD", "cost_type": "free",
        "url_pattern": "https://www.nmpa.gov.cn/",
        "notes": "UDID batch only; JS/captcha portal. Render US IP may be blocked.",
    },
    {
        "source_id": "fcc_oet_eas", "publisher": "FCC OET EAS",
        "source_class": "A", "raw_table": "raw_regulatory", "auth": "none",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://apps.fcc.gov/oetcf/eas/",
        "notes": "Verify EAS Web API contract (KDB 953436) live before building.",
    },
    {
        "source_id": "eu_nando", "publisher": "EU NANDO",
        "source_class": "A", "raw_table": "raw_regulatory", "auth": "none",
        "access_method": "scrape", "connector": None, "wave": 3,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://ec.europa.eu/growth/tools-databases/nando/",
        "notes": "Small, slow; monthly. Inspect new SMCS portal for internal JSON.",
    },
    # ---- Procurement → raw_procurement ----------------------------------------
    {
        "source_id": "usaspending_gov", "publisher": "USASpending.gov",
        "source_class": "A", "raw_table": "raw_procurement", "auth": "none",
        "access_method": "api", "connector": "usaspending", "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://api.usaspending.gov/api/v2/",
        "notes": "~1000 calls/5min. Awards (not solicitations).",
    },
    {
        "source_id": "eu_ted", "publisher": "EU TED (Tenders Electronic Daily)",
        "source_class": "A", "raw_table": "raw_procurement", "auth": "none",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://ted.europa.eu/api/v3.0/",
        "notes": "REST + bulk XML. Complexity is eForms/TED XML schema.",
    },
    {
        "source_id": "ocds_registry", "publisher": "OCDS Registry",
        "source_class": "A", "raw_table": "raw_procurement", "auth": "none",
        "access_method": "api", "connector": "ocds", "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://standard.open-contracting.org/",
        "notes": "One generic OCDS ingester → 100+ gov publishers.",
    },
    {
        "source_id": "sam_gov", "publisher": "SAM.gov",
        "source_class": "A", "raw_table": "raw_procurement", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://api.sam.gov/opportunities/v2/",
        "notes": "Default 10 req/day — need System account for real ingestion.",
    },
    {
        "source_id": "india_gem_cppp", "publisher": "India GeM / CPPP",
        "source_class": "B", "raw_table": "raw_procurement", "auth": "none",
        "access_method": "scrape", "connector": None, "wave": 3,
        "feasibility": "HARD", "cost_type": "free",
        "url_pattern": "https://gem.gov.in/",
        "notes": "No public API; captcha/session; ToS risk.",
    },
    # ---- Filings → raw_filings -----------------------------------------------
    {
        "source_id": "sec_edgar", "publisher": "SEC EDGAR",
        "source_class": "A", "raw_table": "raw_filings", "auth": "none",
        "access_method": "api", "connector": "sec_edgar", "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://data.sec.gov/",
        "notes": "10 req/s. JSON consolidated only; segments need raw XBRL parse.",
    },
    {
        "source_id": "edinet_fsa_api_v2", "publisher": "EDINET FSA (Japan)",
        "source_class": "A", "raw_table": "raw_filings", "auth": "api_key",
        "access_method": "api", "connector": "edinet", "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://api.edinet-fsa.go.jp/api/v2/",
        "notes": "Best structured segments (Murata/TDK). XBRL/CSV, dimensionally tagged.",
    },
    {
        "source_id": "bse_corporate_filings", "publisher": "BSE (India) Corporate Filings",
        "source_class": "A", "raw_table": "raw_filings", "auth": "none",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://api.bseindia.com/",
        "notes": "Quarterly LODR XBRL <24h; geo split in annual notes.",
    },
    {
        "source_id": "uk_companies_house", "publisher": "UK Companies House",
        "source_class": "A", "raw_table": "raw_filings", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://api.companieshouse.gov.uk/",
        "notes": "Registry clean; most filers small/micro = no segments.",
    },
    {
        "source_id": "hkexnews_filings", "publisher": "HKEXnews (Hong Kong)",
        "source_class": "A", "raw_table": "raw_filings", "auth": "none",
        "access_method": "scrape", "connector": None, "wave": 3,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://www1.hkexnews.hk/",
        "notes": "Discovery easy; segment extraction = PDF parse.",
    },
    {
        "source_id": "cninfo_cn", "publisher": "cninfo (China)",
        "source_class": "A", "raw_table": "raw_filings", "auth": "none",
        "access_method": "scrape", "connector": None, "wave": 3,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://www.cninfo.com.cn/",
        "notes": "Needs realistic headers + CN/HK proxy from Render.",
    },
    # ---- Transcripts → raw_transcripts ----------------------------------------
    {
        "source_id": "roic_ai", "publisher": "Roic.ai",
        "source_class": "B", "raw_table": "raw_transcripts", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://api.roic.ai/",
        "notes": "5 req/min, 2-yr history. Best free sanctioned option.",
    },
    {
        "source_id": "api_ninjas", "publisher": "API Ninjas",
        "source_class": "B", "raw_table": "raw_transcripts", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "EASY", "cost_type": "paid",
        "url_pattern": "https://api.api-ninjas.com/v1/",
        "notes": "Developer tier forbids storage → need Business for JSONB.",
    },
    {
        "source_id": "financial_modeling_prep", "publisher": "Financial Modeling Prep",
        "source_class": "B", "raw_table": "raw_transcripts", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "EASY", "cost_type": "paid",
        "url_pattern": "https://financialmodelingprep.com/api/v3/",
        "notes": "~$22–149/mo. Also structured-segment backbone.",
    },
    {
        "source_id": "earningscalls_dev", "publisher": "EarningsCalls.dev",
        "source_class": "B", "raw_table": "raw_transcripts", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "EASY", "cost_type": "paid",
        "url_pattern": "https://earningscalls.dev/api/",
        "notes": "MCP-ready. Verify free quota.",
    },
    # ---- Macro & national stats → raw_external_metrics ------------------------
    {
        "source_id": "world_bank_indicators", "publisher": "World Bank Indicators",
        "source_class": "C", "raw_table": "raw_external_metrics", "auth": "none",
        "access_method": "api", "connector": "worldbank", "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://api.worldbank.org/v2/",
        "notes": "Cleanest. ?format=json, response [meta,data].",
    },
    {
        "source_id": "fred_stlouisfed", "publisher": "FRED (St. Louis Fed)",
        "source_class": "C", "raw_table": "raw_external_metrics", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://api.stlouisfed.org/fred/",
        "notes": "120/min. Confirm v1 vs v2 auth.",
    },
    {
        "source_id": "who_gho_odata", "publisher": "WHO GHO (OData)",
        "source_class": "C", "raw_table": "raw_external_metrics", "auth": "none",
        "access_method": "api", "connector": "who_gho", "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://ghoapi.azureedge.net/api/",
        "notes": "Host on azureedge.net — make base URL configurable.",
    },
    {
        "source_id": "eurostat_dissemination_api", "publisher": "Eurostat Dissemination API",
        "source_class": "C", "raw_table": "raw_external_metrics", "auth": "none",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/",
        "notes": "pyjstat; per-query size cap, async holding.",
    },
    {
        "source_id": "imf_data_sdmx_api", "publisher": "IMF Data (SDMX API)",
        "source_class": "C", "raw_table": "raw_external_metrics", "auth": "none",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://dataservices.imf.org/REST/SDMX_JSON.svc/",
        "notes": "Old host decommissioned Nov 2025. Use DataMapper (easy JSON) first.",
    },
    {
        "source_id": "oecd_sdmx_api", "publisher": "OECD (SDMX API)",
        "source_class": "C", "raw_table": "raw_external_metrics", "auth": "none",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://sdmx.oecd.org/public/rest/",
        "notes": "Migrated to sdmx.oecd.org — dataflow IDs changed.",
    },
    {
        "source_id": "un_sdg_indicators", "publisher": "UN SDG Indicators",
        "source_class": "C", "raw_table": "raw_external_metrics", "auth": "none",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://unstats.un.org/SDGAPI/v1/",
        "notes": "Heavy params/pagination.",
    },
    {
        "source_id": "malaysia_dosm_opendosm", "publisher": "Malaysia DOSM (OpenDOSM)",
        "source_class": "B", "raw_table": "raw_external_metrics", "auth": "none",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://api.data.gov.my/",
        "notes": "Cleanest national agency; read static Parquet for verbatim.",
    },
    {
        "source_id": "singapore_singstat", "publisher": "Singapore SingStat",
        "source_class": "B", "raw_table": "raw_external_metrics", "auth": "none",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://tablebuilder.singstat.gov.sg/api/",
        "notes": "Descriptive User-Agent mandatory.",
    },
    {
        "source_id": "india_data_gov_in_ogd", "publisher": "India data.gov.in (OGD)",
        "source_class": "B", "raw_table": "raw_external_metrics", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://api.data.gov.in/resource/",
        "notes": "Per-dataset resource-id discovery; MoSPI partly separate.",
    },
    {
        "source_id": "vietnam_nso_pxweb", "publisher": "Vietnam NSO (PxWeb)",
        "source_class": "B", "raw_table": "raw_external_metrics", "auth": "none",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://www.gso.gov.vn/",
        "notes": "Verify /api/v1/ enabled + TLS cert handling.",
    },
    {
        "source_id": "china_nbs_national_data", "publisher": "China NBS National Data",
        "source_class": "B", "raw_table": "raw_external_metrics", "auth": "none",
        "access_method": "scrape", "connector": None, "wave": 3,
        "feasibility": "HARD", "cost_type": "free",
        "url_pattern": "https://data.stats.gov.cn/",
        "notes": "easyquery.htm; geoblock risk from Render → proxy/intermediary.",
    },
    # ---- Industry reports → raw_industry_reports ------------------------------
    {
        "source_id": "wsts_historical_billings", "publisher": "WSTS Historical Billings",
        "source_class": "B", "raw_table": "raw_industry_reports", "auth": "none",
        "access_method": "manual_upload", "connector": None, "wave": 2,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://www.wsts.org/",
        "notes": "Single stable XLSX; internal-use only, don't redistribute.",
    },
    {
        "source_id": "sia_global_sales_report", "publisher": "SIA Global Sales Report",
        "source_class": "B", "raw_table": "raw_industry_reports", "auth": "none",
        "access_method": "scrape", "connector": None, "wave": 3,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://www.semiconductors.org/",
        "notes": "Cloudflare 403 — browser UA. Republishes WSTS headline.",
    },
    {
        "source_id": "semi_equipment_billings", "publisher": "SEMI Equipment Billings",
        "source_class": "B", "raw_table": "raw_industry_reports", "auth": "none",
        "access_method": "scrape", "connector": None, "wave": 3,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://www.semi.org/",
        "notes": "Use PR Newswire mirror; detail behind paid EMDS.",
    },
    {
        "source_id": "oica_vehicle_statistics", "publisher": "OICA Vehicle Statistics",
        "source_class": "B", "raw_table": "raw_industry_reports", "auth": "none",
        "access_method": "scrape", "connector": None, "wave": 3,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://www.oica.net/",
        "notes": "Annual, long lag.",
    },
    {
        "source_id": "acea_vehicle_registrations", "publisher": "ACEA Vehicle Registrations",
        "source_class": "B", "raw_table": "raw_industry_reports", "auth": "none",
        "access_method": "scrape", "connector": None, "wave": 3,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://www.acea.auto/",
        "notes": "Prefer ECB Data Portal dataset CAR (proper API) over scraping.",
    },
    {
        "source_id": "idc_data", "publisher": "IDC",
        "source_class": "B", "raw_table": "raw_industry_reports", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 4,
        "feasibility": "MEDIUM", "cost_type": "paid",
        "url_pattern": None,
        "notes": "Real API but subscriber-provisioned.",
    },
    {
        "source_id": "euromonitor_passport", "publisher": "Euromonitor Passport",
        "source_class": "B", "raw_table": "raw_industry_reports", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 4,
        "feasibility": "MEDIUM", "cost_type": "paid",
        "url_pattern": None,
        "notes": "Best paid ergonomics; entitlement-gated.",
    },
    {
        "source_id": "sp_global_market_intelligence", "publisher": "S&P Global Market Intelligence",
        "source_class": "B", "raw_table": "raw_industry_reports", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 4,
        "feasibility": "MEDIUM", "cost_type": "paid",
        "url_pattern": None,
        "notes": "Per-dataset entitlements.",
    },
    # ---- Patents → raw_patents -----------------------------------------------
    {
        "source_id": "uspto_patentsview", "publisher": "USPTO PatentsView",
        "source_class": "C", "raw_table": "raw_patents", "auth": "api_key",
        "access_method": "api", "connector": "uspto_patentsview", "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://search.patentsview.org/api/v1/",
        "notes": "Legacy host sunset May 2025 → use search.patentsview.org. ~45/min.",
    },
    {
        "source_id": "uspto_odp", "publisher": "USPTO ODP (bulk)",
        "source_class": "C", "raw_table": "raw_patents", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://developer.uspto.gov/api-catalog/",
        "notes": "Sign-in change ~Jun 18 2026; bulk 20 dl/file/yr.",
    },
    {
        "source_id": "epo_ops", "publisher": "EPO Open Patent Services",
        "source_class": "C", "raw_table": "raw_patents", "auth": "oauth",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "https://ops.epo.org/3.2/rest-services/",
        "notes": "Token ~20min TTL; XML primary; EP/global. ~4GB/wk free.",
    },
    {
        "source_id": "lens_org", "publisher": "The Lens",
        "source_class": "C", "raw_table": "raw_patents", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 4,
        "feasibility": "EASY", "cost_type": "paid",
        "url_pattern": "https://api.lens.org/",
        "notes": "Cleanest global JSON; production needs paid plan.",
    },
    # ---- Shipments → raw_shipments -------------------------------------------
    {
        "source_id": "oec_bill_of_lading", "publisher": "OEC Bill of Lading",
        "source_class": "B", "raw_table": "raw_shipments", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 4,
        "feasibility": "EASY", "cost_type": "paid",
        "url_pattern": "https://oec.world/api/",
        "notes": "$299–1,999/mo. Confirm company-level on Pro vs Premium.",
    },
    {
        "source_id": "importgenius_api", "publisher": "ImportGenius",
        "source_class": "B", "raw_table": "raw_shipments", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 4,
        "feasibility": "EASY", "cost_type": "paid",
        "url_pattern": None,
        "notes": "~$899/user/mo. 24+ jurisdictions.",
    },
    {
        "source_id": "importyeti_api", "publisher": "ImportYeti",
        "source_class": "B", "raw_table": "raw_shipments", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 4,
        "feasibility": "MEDIUM", "cost_type": "paid",
        "url_pattern": None,
        "notes": "Official beta API; endpoints/pricing unverified.",
    },
    # ---- Standards → raw_standards -------------------------------------------
    {
        "source_id": "3gpp", "publisher": "3GPP",
        "source_class": "B", "raw_table": "raw_standards", "auth": "none",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://www.3gpp.org/ftp/Specs/",
        "notes": "Work Plan .xlsx + OpenAPI YAML; only truly structured standards source.",
    },
    {
        "source_id": "etsi", "publisher": "ETSI",
        "source_class": "B", "raw_table": "raw_standards", "auth": "none",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://www.etsi.org/",
        "notes": "50-result export cap; also hosts 3GPP roster.",
    },
    {
        "source_id": "jedec", "publisher": "JEDEC",
        "source_class": "B", "raw_table": "raw_standards", "auth": "none",
        "access_method": "scrape", "connector": None, "wave": 3,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://www.jedec.org/",
        "notes": "Member list = single HTML page.",
    },
    # ---- News & M&A → raw_news -----------------------------------------------
    {
        "source_id": "gdelt_doc_api", "publisher": "GDELT Document API",
        "source_class": "C", "raw_table": "raw_news", "auth": "none",
        "access_method": "api", "connector": "gdelt", "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://api.gdeltproject.org/api/v2/doc/doc",
        "notes": "250 rec/call, paginate by datetime.",
    },
    {
        "source_id": "gdelt_files", "publisher": "GDELT Bulk Files",
        "source_class": "C", "raw_table": "raw_news", "auth": "none",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "MEDIUM", "cost_type": "free",
        "url_pattern": "http://data.gdeltproject.org/gdeltv2/",
        "notes": "HTTP-only host; validate MD5. Bulk CSV.zip 15min cadence.",
    },
    {
        "source_id": "prnewswire_rss", "publisher": "PR Newswire (RSS)",
        "source_class": "C", "raw_table": "raw_news", "auth": "none",
        "access_method": "api", "connector": "newswire_rss", "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://www.prnewswire.com/rss/",
        "notes": "Headlines only; full body needs page fetch.",
    },
    {
        "source_id": "businesswire_rss", "publisher": "Business Wire (RSS)",
        "source_class": "C", "raw_table": "raw_news", "auth": "none",
        "access_method": "api", "connector": "newswire_rss", "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://feed.businesswire.com/rss/home/?rss=G23",
        "notes": "Headlines only; full body needs page fetch.",
    },
    {
        "source_id": "globenewswire_rss", "publisher": "GlobeNewswire (RSS)",
        "source_class": "C", "raw_table": "raw_news", "auth": "none",
        "access_method": "api", "connector": "newswire_rss", "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://www.globenewswire.com/RssFeed/subjectcode/24-Industrial+Goods",
        "notes": "Headlines only; full body needs page fetch.",
    },
    {
        "source_id": "crunchbase_api", "publisher": "Crunchbase",
        "source_class": "C", "raw_table": "raw_news", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 4,
        "feasibility": "EASY", "cost_type": "paid",
        "url_pattern": "https://api.crunchbase.com/api/v4/",
        "notes": "Free tier removed 2025. Funding/M&A enrichment.",
    },
    {
        "source_id": "opencorporates_api", "publisher": "OpenCorporates",
        "source_class": "C", "raw_table": "raw_news", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 2,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://api.opencorporates.com/v0.4/",
        "notes": "Public-benefit application, not instant.",
    },
    # ---- Hiring/capacity signals → raw_signals --------------------------------
    {
        "source_id": "bls_publicdata_api_v2", "publisher": "BLS Public Data API v2",
        "source_class": "C", "raw_table": "raw_signals", "auth": "api_key",
        "access_method": "api", "connector": "bls", "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://api.bls.gov/publicAPI/v2/timeseries/data/",
        "notes": "JOLTS; 21-char series IDs; 500 q/day, 50 series/q.",
    },
    {
        "source_id": "usajobs_search_api", "publisher": "USAJOBS Search API",
        "source_class": "C", "raw_table": "raw_signals", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://data.usajobs.gov/api/",
        "notes": "UA=email + Authorization-Key; ≤500/page, ~10k cap.",
    },
    {
        "source_id": "adzuna", "publisher": "Adzuna",
        "source_class": "C", "raw_table": "raw_signals", "auth": "api_key",
        "access_method": "api", "connector": None, "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://api.adzuna.com/v1/api/jobs/",
        "notes": "Broad aggregator; verify free quota.",
    },
    {
        "source_id": "greenhouse_ats", "publisher": "Greenhouse ATS",
        "source_class": "C", "raw_table": "raw_signals", "auth": "none",
        "access_method": "api", "connector": "ats_family", "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://boards-api.greenhouse.io/v1/boards/",
        "notes": "ATS family; one generic connector + companies×ATS slug table.",
    },
    {
        "source_id": "lever_ats", "publisher": "Lever ATS",
        "source_class": "C", "raw_table": "raw_signals", "auth": "none",
        "access_method": "api", "connector": "ats_family", "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://api.lever.co/v0/postings/",
        "notes": "ATS family connector.",
    },
    {
        "source_id": "ashby_ats", "publisher": "Ashby ATS",
        "source_class": "C", "raw_table": "raw_signals", "auth": "none",
        "access_method": "api", "connector": "ats_family", "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://api.ashbyhq.com/posting-api/job-board/",
        "notes": "ATS family connector.",
    },
    {
        "source_id": "smartrecruiters_ats", "publisher": "SmartRecruiters ATS",
        "source_class": "C", "raw_table": "raw_signals", "auth": "none",
        "access_method": "api", "connector": "ats_family", "wave": 1,
        "feasibility": "EASY", "cost_type": "free",
        "url_pattern": "https://api.smartrecruiters.com/v1/companies/",
        "notes": "ATS family connector.",
    },
]

# Index for O(1) lookup by source_id.
_CATALOG_INDEX: dict[str, dict[str, Any]] = {e["source_id"]: e for e in _CATALOG}

# Columns of raw_* tables keyed by table name — used by suggest-mapping prompt.
_RAW_TABLE_COLUMNS: dict[str, list[str]] = {
    "raw_trade_flows": ["reporter", "partner", "hs_code", "hs_version", "flow", "period", "value_usd", "qty", "qty_unit"],
    "raw_regulatory": ["registration_id", "holder", "product_code", "country", "status"],
    "raw_filings": ["filer", "ticker", "period", "segment", "geography", "revenue_usd", "doc_url"],
    "raw_transcripts": ["company", "period", "content"],
    "raw_shipments": ["shipper", "consignee", "hs_code", "origin", "dest", "value_usd", "period"],
    "raw_external_metrics": ["indicator", "country", "period", "value", "unit"],
    "raw_industry_reports": ["publisher", "market", "period", "tam_usd", "doc_url"],
    "raw_patents": ["patent_id", "assignee", "cpc", "filing_date", "country"],
    "raw_procurement": ["award_id", "buyer", "supplier", "country", "value_usd", "period"],
    "raw_standards": ["body", "member", "membership_tier"],
    "raw_news": ["headline", "url", "published_at", "entity", "snippet"],
    "raw_signals": ["company", "signal_type", "country", "period", "value"],
}


# =========================================================================== #
# Request / response schemas (local to this router)
# =========================================================================== #

class CatalogEntryOut(BaseModel):
    """One entry in the static connector catalog, merged with live DB state."""

    source_id: str
    publisher: str
    source_class: Literal["A", "B", "C"] | None = None
    raw_table: str | None = None
    auth: str | None = None                    # none | api_key | oauth | login | scrape
    access_method: str | None = None           # api | scrape | web_search | manual_upload
    connector: str | None = None               # module name in connectors/
    url_pattern: str | None = None
    wave: int | None = None                    # 1=EASY, 2=MEDIUM, 3=HARD, 4=paid
    feasibility: str | None = None             # EASY | MEDIUM | HARD
    cost_type: str | None = None               # free | paid | freemium
    notes: str | None = None
    # Live state (present only when the source exists in the DB)
    in_db: bool = False
    enabled: bool | None = None
    has_credential: bool = False
    last_probe_status: str | None = None
    last_probe_at: datetime | None = None
    last_probe_detail: str | None = None
    monthly_budget: Decimal | None = None
    quota_ceiling: int | None = None


class ConnectorHealthOut(BaseModel):
    """Per-source health summary for the connector health dashboard."""

    source_id: str
    publisher: str
    enabled: bool
    last_probe_status: str | None = None        # 7-state taxonomy
    last_probe_at: datetime | None = None
    last_probe_detail: str | None = None
    access_method: str | None = None
    monthly_budget: Decimal | None = None
    quota_ceiling: int | None = None
    budget_warning: bool = False                # True when quota exhausted or near ceiling
    raw_table: str | None = None


class ConnectorSelectIn(BaseModel):
    """Body for POST /connectors — select an existing catalog connector."""

    source_id: str = Field(..., description="The catalog source_id to enable.")
    monthly_budget: Decimal | None = Field(
        default=None, description="Optional spend ceiling (USD/month) for budget pre-warning."
    )
    quota_ceiling: int | None = Field(
        default=None, description="Optional API call ceiling for quota pre-warning."
    )
    notes: str | None = None


class FieldMapping(BaseModel):
    """One entry in a declarative field-map for a generic-REST source."""

    source_field: str = Field(..., description="JSON path in the source response (dot notation).")
    target_column: str = Field(..., description="Column in the raw_* table this maps to.")
    transform: str | None = Field(
        default=None, description="Optional Python-style transform hint (e.g. 'float(x)')."
    )


class CustomSourceIn(BaseModel):
    """Body for POST /connectors/custom — user-defined declarative REST source."""

    source_id: str = Field(..., description="A unique slug for this source (e.g. my_api).")
    publisher: str
    url_pattern: str = Field(..., description="Base URL or templated endpoint pattern.")
    auth: Literal["none", "api_key", "oauth", "login", "scrape"] = "none"
    source_class: Literal["A", "B", "C"] = "C"
    raw_table: str = Field(
        ..., description="Target raw_* table. Must be one of the 12 raw tables."
    )
    refresh_cadence: str | None = None
    monthly_budget: Decimal | None = None
    quota_ceiling: int | None = None
    notes: str | None = None
    field_map: list[FieldMapping] = Field(
        default_factory=list,
        description="Declarative field-map persisted as JSON in notes (drives generic_rest connector).",
    )


class CredentialIn(BaseModel):
    """Admin-only body for POST /connectors/{source_id}/credential (write-only)."""

    secret: str = Field(..., min_length=1, description="The API key / token / password.")
    label: str | None = Field(
        default=None, description="Human-readable label for the credential (stored in notes)."
    )


class CredentialOut(BaseModel):
    """Response after a credential write (never returns the secret)."""

    cred_ref: str
    source_id: str
    action: Literal["added", "rotated"]
    stored_at: datetime


class ProbeOut(BaseModel):
    """Result of a /probe call."""

    source_id: str
    status: str                 # ProbeStatus literal
    detail: str
    sample: Any | None = None
    probed_at: datetime


class SuggestMappingIn(BaseModel):
    """Body for POST /connectors/{source_id}/suggest-mapping."""

    sample_payload: dict[str, Any] = Field(
        ..., description="A representative verbatim JSON payload from the source."
    )
    target_table: str = Field(
        ..., description="The raw_* table this source should land in."
    )


class SuggestMappingOut(BaseModel):
    """AI-suggested field mapping from source payload keys to raw_* table columns."""

    source_id: str
    target_table: str
    suggested_mapping: list[FieldMapping]
    confidence_notes: str
    model_used: str | None = None


# =========================================================================== #
# Internal helpers
# =========================================================================== #

def _load_source_row(session: Session, source_id: str, engagement_id: str) -> dict[str, Any]:
    """Load one sources row (scoped to the active engagement) as a plain dict,
    aliasing the 'class' column."""
    row = session.execute(
        text("""
            SELECT source_id, publisher, url_pattern, auth, auth_secret_ref,
                   class AS source_class, connector, refresh_cadence, raw_table,
                   access_method, discovered, monthly_budget, quota_ceiling,
                   last_probe_status, last_probe_at, last_probe_detail,
                   enabled, notes, created_at
            FROM sources WHERE source_id = :sid AND engagement_id = :eng
        """),
        {"sid": source_id, "eng": engagement_id},
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Source '{source_id}' not found in the sources table.",
        )
    return dict(row._mapping)


def _load_all_source_rows(session: Session, engagement_id: str) -> list[dict[str, Any]]:
    """Return all sources rows for the active engagement as plain dicts."""
    rows = session.execute(
        text("""
            SELECT source_id, publisher, url_pattern, auth, auth_secret_ref,
                   class AS source_class, connector, refresh_cadence, raw_table,
                   access_method, discovered, monthly_budget, quota_ceiling,
                   last_probe_status, last_probe_at, last_probe_detail,
                   enabled, notes, created_at
            FROM sources WHERE engagement_id = :eng ORDER BY source_id
        """),
        {"eng": engagement_id},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def _budget_warning(row: dict[str, Any]) -> bool:
    """Return True when the source's last probe was QUOTA_EXHAUSTED."""
    # Emit an advisory when the source is known to be quota-exhausted.
    # (Actual spend tracking lives in the pipeline; here we surface the 7-state signal.)
    return row.get("last_probe_status") == "QUOTA_EXHAUSTED"


def _source_admin_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Build the frontend ``Source``-shaped dict from a sources row mapping.

    Expects the DB ``class`` column surfaced under ``source_class`` (as produced
    by the aliased SELECT in ``_load_source_row`` / ``list_sources``). Emits it
    back under the ``class`` key and attaches the computed ``budget_warning``
    advisory. Shared by the list endpoint and the per-source detail/enable/disable
    endpoints so all four return the exact same shape.
    """
    return {
        "source_id": d["source_id"],
        "publisher": d["publisher"],
        "url_pattern": d.get("url_pattern"),
        "auth": d.get("auth"),
        "auth_secret_ref": d.get("auth_secret_ref"),
        "class": d.get("source_class"),
        "connector": d.get("connector"),
        "refresh_cadence": d.get("refresh_cadence"),
        "raw_table": d.get("raw_table"),
        "access_method": d.get("access_method"),
        "discovered": bool(d.get("discovered")),
        "monthly_budget": d.get("monthly_budget"),
        "quota_ceiling": d.get("quota_ceiling"),
        "last_probe_status": d.get("last_probe_status"),
        "last_probe_at": d.get("last_probe_at"),
        "last_probe_detail": d.get("last_probe_detail"),
        "enabled": bool(d.get("enabled")),
        "notes": d.get("notes"),
        "budget_warning": _budget_warning(d),
    }


def _probe_url_blocked(url: str) -> str | None:
    """SSRF guard for the generic HTTP probe: reject non-public targets.

    Custom REST sources let an admin register an arbitrary ``url_pattern``,
    which the probe then fetches server-side (optionally with the stored
    credential attached). Without this check that is a server-side request
    forgery primitive against the hosting network (cloud metadata endpoints,
    internal services). Returns a human-readable reason when blocked, or
    ``None`` when the URL is safe to probe.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return "unparseable URL"
    if parsed.scheme not in ("http", "https"):
        return f"unsupported scheme {parsed.scheme!r}"
    host = parsed.hostname
    if not host:
        return "URL has no host"
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        return f"cannot resolve host {host!r}"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return f"host {host!r} resolves to a non-public address ({ip})"
    return None


def _run_probe(source_row: dict[str, Any], credential: str | None) -> ProbeResult:
    """Run the connector probe (or a generic HTTP probe when no connector is registered)."""
    discover()
    connector_name = source_row.get("connector")
    connector_obj = get_connector(source_row, credential) if connector_name else None

    if connector_obj is not None:
        try:
            return connector_obj.probe()
        except Exception as exc:  # noqa: BLE001 — probe must not crash the API
            pstatus, detail = classify_exception(exc)
            return ProbeResult(pstatus, detail, None)
        finally:
            try:
                connector_obj.close()
            except Exception:  # noqa: BLE001
                pass

    # Generic HTTP probe when no specific connector is registered.
    url = source_row.get("url_pattern")
    if not url:
        return ProbeResult("UNREACHABLE", "no url_pattern configured and no connector registered", None)
    # KNOWN RESIDUAL RISK (accepted): the guard resolves DNS here while httpx
    # re-resolves at request time, so a rebinding DNS server could race the
    # check (TOC/TOU). Closing it fully needs an IP-pinned transport. The
    # endpoint is owner/admin-gated when auth is configured; deployments
    # without WorkOS must treat the whole admin surface as untrusted-exposed.
    blocked_reason = _probe_url_blocked(url)
    if blocked_reason:
        return ProbeResult("UNREACHABLE", f"probe blocked: {blocked_reason}", None)
    headers: dict[str, str] = {}
    if credential:
        auth_method = (source_row.get("auth") or "none").lower()
        if auth_method == "api_key":
            headers["X-API-Key"] = credential
    try:
        # Redirects are NOT followed: a public host 30x-ing to an internal
        # address would bypass the SSRF guard above.
        resp = httpx.get(url, headers=headers, timeout=10.0, follow_redirects=False)
        if 200 <= resp.status_code < 300:
            try:
                sample: Any = resp.json()
                if isinstance(sample, list):
                    sample = sample[:1]
                elif isinstance(sample, dict):
                    sample = {k: sample[k] for k in list(sample)[:5]}
            except Exception:  # noqa: BLE001
                sample = resp.text[:300]
            return ProbeResult("OK", f"HTTP {resp.status_code}", sample)
        if 300 <= resp.status_code < 400:
            # Endpoint is reachable; redirects are deliberately not followed
            # (SSRF guard) so treat the hop itself as a healthy signal.
            return ProbeResult(
                "OK",
                f"HTTP {resp.status_code} redirect (not followed) -> "
                f"{resp.headers.get('location', '?')[:120]}",
                None,
            )
        pstatus = classify_http_error(resp.status_code, resp.text)
        return ProbeResult(pstatus, f"HTTP {resp.status_code}: {resp.text[:200]}", None)
    except httpx.TimeoutException:
        return ProbeResult("UNREACHABLE", "connection timed out", None)
    except httpx.TransportError as exc:
        return ProbeResult("UNREACHABLE", f"transport error: {exc}", None)


def _write_probe_result(
    session: Session, source_id: str, result: ProbeResult, probed_at: datetime,
    engagement_id: str,
) -> None:
    """Persist the probe outcome into sources.last_probe_* (engagement-scoped)."""
    session.execute(
        text("""
            UPDATE sources
            SET last_probe_status = :status,
                last_probe_at     = :at,
                last_probe_detail = :detail
            WHERE source_id = :sid AND engagement_id = :eng
        """),
        {
            "status": result.status,
            "at": probed_at,
            "detail": result.detail[:2000] if result.detail else None,
            "sid": source_id,
            "eng": engagement_id,
        },
    )


def _merge_catalog_with_db(
    catalog_entries: list[dict[str, Any]],
    db_rows: list[dict[str, Any]],
) -> list[CatalogEntryOut]:
    """Merge static catalog entries with live DB state."""
    db_index = {r["source_id"]: r for r in db_rows}
    seen: set[str] = set()
    result: list[CatalogEntryOut] = []

    for entry in catalog_entries:
        sid = entry["source_id"]
        seen.add(sid)
        db_row = db_index.get(sid)
        out = CatalogEntryOut(
            source_id=sid,
            publisher=entry["publisher"],
            source_class=entry.get("source_class"),
            raw_table=entry.get("raw_table"),
            auth=entry.get("auth"),
            access_method=entry.get("access_method"),
            connector=entry.get("connector"),
            url_pattern=entry.get("url_pattern"),
            wave=entry.get("wave"),
            feasibility=entry.get("feasibility"),
            cost_type=entry.get("cost_type"),
            notes=entry.get("notes"),
        )
        if db_row:
            out.in_db = True
            out.enabled = db_row.get("enabled", False)
            out.has_credential = bool(db_row.get("auth_secret_ref"))
            out.last_probe_status = db_row.get("last_probe_status")
            out.last_probe_at = db_row.get("last_probe_at")
            out.last_probe_detail = db_row.get("last_probe_detail")
            out.monthly_budget = db_row.get("monthly_budget")
            out.quota_ceiling = db_row.get("quota_ceiling")
        result.append(out)

    # Append custom/discovered sources not in the static catalog.
    for db_row in db_rows:
        if db_row["source_id"] not in seen:
            result.append(
                CatalogEntryOut(
                    source_id=db_row["source_id"],
                    publisher=db_row.get("publisher", ""),
                    source_class=db_row.get("source_class"),
                    raw_table=db_row.get("raw_table"),
                    auth=db_row.get("auth"),
                    access_method=db_row.get("access_method"),
                    connector=db_row.get("connector"),
                    url_pattern=db_row.get("url_pattern"),
                    notes=db_row.get("notes"),
                    in_db=True,
                    enabled=db_row.get("enabled", False),
                    has_credential=bool(db_row.get("auth_secret_ref")),
                    last_probe_status=db_row.get("last_probe_status"),
                    last_probe_at=db_row.get("last_probe_at"),
                    last_probe_detail=db_row.get("last_probe_detail"),
                    monthly_budget=db_row.get("monthly_budget"),
                    quota_ceiling=db_row.get("quota_ceiling"),
                )
            )
    return result


def _call_ai_mapping(
    source_id: str,
    sample_payload: dict[str, Any],
    target_table: str,
) -> SuggestMappingOut:
    """Call the Anthropic API to suggest field mappings.

    Falls back to a best-effort heuristic if the API key is absent.
    """
    target_cols = _RAW_TABLE_COLUMNS.get(target_table, [])
    col_list = ", ".join(target_cols) if target_cols else "(unknown table — check raw_table name)"

    sample_text = json.dumps(sample_payload, indent=2, default=str)[:2000]

    prompt = textwrap.dedent(f"""
        You are a data integration assistant for a market-research platform.

        A user has configured a new data source (source_id="{source_id}") that lands
        into the PostgreSQL table `{target_table}`.

        The destination table has these typed columns (beyond the common PK/source_id/raw_json):
        {col_list}

        Here is a sample JSON payload from the source:
        ```json
        {sample_text}
        ```

        Task: suggest a field mapping from source payload keys/paths to destination columns.
        Use dot notation for nested keys (e.g. "trade.value" → "value_usd").
        Only map keys that clearly correspond to a destination column.
        For each mapping, include a brief transform hint if the types don't match
        (e.g. the source has a string date and the target expects a numeric year).

        Return ONLY a JSON object in this exact shape (no extra keys, no prose):
        {{
          "mappings": [
            {{"source_field": "...", "target_column": "...", "transform": "..." }},
            ...
          ],
          "confidence_notes": "Brief explanation of confidence and any caveats."
        }}
    """).strip()

    api_key = settings.ANTHROPIC_API_KEY
    if not api_key:
        # Heuristic fallback: match by name similarity.
        mappings = []
        for key in _flatten_keys(sample_payload):
            for col in target_cols:
                if _names_similar(key, col):
                    mappings.append(
                        FieldMapping(source_field=key, target_column=col, transform=None)
                    )
                    break
        return SuggestMappingOut(
            source_id=source_id,
            target_table=target_table,
            suggested_mapping=mappings,
            confidence_notes=(
                "Heuristic name-similarity match (ANTHROPIC_API_KEY not configured). "
                "Review all suggestions manually."
            ),
            model_used=None,
        )

    # Call the Anthropic Messages API via httpx (avoids requiring the SDK package).
    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-3-5-haiku-20241022",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        raw_text: str = resp.json()["content"][0]["text"]
        # Strip markdown code fences if present.
        raw_text = raw_text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        parsed = json.loads(raw_text)
        mappings = [
            FieldMapping(
                source_field=m["source_field"],
                target_column=m["target_column"],
                transform=m.get("transform"),
            )
            for m in parsed.get("mappings", [])
        ]
        return SuggestMappingOut(
            source_id=source_id,
            target_table=target_table,
            suggested_mapping=mappings,
            confidence_notes=parsed.get("confidence_notes", ""),
            model_used="claude-3-5-haiku-20241022",
        )
    except Exception as exc:  # noqa: BLE001 — AI mapping is best-effort
        logger.warning("AI mapping call failed for %s: %s", source_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AI mapping service unavailable: {exc}",
        )


def _flatten_keys(payload: dict[str, Any], prefix: str = "") -> list[str]:
    """Recursively collect dot-notation keys from a nested dict (max depth 3)."""
    keys: list[str] = []
    for k, v in payload.items():
        full = f"{prefix}.{k}" if prefix else k
        keys.append(full)
        if isinstance(v, dict) and prefix.count(".") < 2:
            keys.extend(_flatten_keys(v, full))
    return keys


def _names_similar(source_key: str, col: str) -> bool:
    """True when source key and column name share significant substring overlap."""
    sk = source_key.lower().split(".")[-1].replace("_", "").replace("-", "")
    cc = col.lower().replace("_", "").replace("-", "")
    return sk == cc or (len(cc) >= 4 and (sk in cc or cc in sk))


# =========================================================================== #
# Endpoints
# =========================================================================== #

@router.get(
    "/catalog",
    response_model=list[CatalogEntryOut],
    summary="Connector catalog (static ground truth merged with live DB state)",
)
def get_catalog(
    db: DbSession,
    engagement_id: EngagementDep,
    _user: CurrentUserDep,
    wave: int | None = None,
    feasibility: str | None = None,
    in_db_only: bool = False,
) -> list[CatalogEntryOut]:
    """Return the full connector catalog with live state overlaid from the sources table.

    Filter parameters:
    * ``wave`` — filter to a specific build wave (1=EASY/free, 4=paid).
    * ``feasibility`` — EASY | MEDIUM | HARD.
    * ``in_db_only`` — only return sources already added to the database.
    """
    db_rows = _load_all_source_rows(db, engagement_id)
    entries = _merge_catalog_with_db(_CATALOG, db_rows)

    if wave is not None:
        entries = [e for e in entries if e.wave == wave]
    if feasibility:
        entries = [e for e in entries if e.feasibility == feasibility.upper()]
    if in_db_only:
        entries = [e for e in entries if e.in_db]

    return entries


@router.get(
    "/health",
    response_model=list[ConnectorHealthOut],
    summary="Connector health — last_probe_* and budget warnings for all sources",
)
def get_health(
    db: DbSession,
    engagement_id: EngagementDep,
    _user: CurrentUserDep,
    enabled_only: bool = True,
) -> list[ConnectorHealthOut]:
    """Return probe health and budget-warning state for every source in the DB.

    ``budget_warning=True`` when ``last_probe_status == 'QUOTA_EXHAUSTED'``
    (the pipeline 7-state taxonomy surfaces this before a hard block).
    """
    rows = _load_all_source_rows(db, engagement_id)
    out: list[ConnectorHealthOut] = []
    for row in rows:
        if enabled_only and not row.get("enabled", True):
            continue
        out.append(
            ConnectorHealthOut(
                source_id=row["source_id"],
                publisher=row.get("publisher", ""),
                enabled=bool(row.get("enabled", True)),
                last_probe_status=row.get("last_probe_status"),
                last_probe_at=row.get("last_probe_at"),
                last_probe_detail=row.get("last_probe_detail"),
                access_method=row.get("access_method"),
                monthly_budget=row.get("monthly_budget"),
                quota_ceiling=row.get("quota_ceiling"),
                budget_warning=_budget_warning(row),
                raw_table=row.get("raw_table"),
            )
        )
    return out


@router.get(
    "",
    response_model=None,
    summary="List all configured sources (sources table) for the connectors admin view",
)
def list_sources(
    db: DbSession, engagement_id: EngagementDep, _user: CurrentUserDep
) -> list[dict[str, Any]]:
    """Return every row in the ``sources`` table in the shape the frontend
    ``Source`` type expects.

    The DB ``class`` column is surfaced under the ``class`` key (not the aliased
    ``source_class``), and the computed ``budget_warning`` advisory is attached.
    Used by the connectors admin screen (``/connectors``) and the assumptions
    ledger source picker. Ordered enabled-first, then by ``source_id``.
    """
    rows = db.execute(
        text("""
            SELECT source_id, publisher, url_pattern, auth, auth_secret_ref,
                   class AS source_class, connector, refresh_cadence, raw_table,
                   access_method, discovered, monthly_budget, quota_ceiling,
                   last_probe_status, last_probe_at, last_probe_detail,
                   enabled, notes
            FROM sources
            WHERE engagement_id = :eng
            ORDER BY enabled DESC, source_id
        """),
        {"eng": engagement_id},
    ).mappings().all()

    return [_source_admin_dict(dict(r)) for r in rows]


@router.get(
    "/{source_id}",
    response_model=None,
    summary="Fetch a single configured source (sources table) for the admin detail view",
)
def get_source(
    source_id: str, db: DbSession, engagement_id: EngagementDep, _user: CurrentUserDep
) -> dict[str, Any]:
    """Return one ``sources`` row in the same shape as the list endpoint's items.

    Used by the connectors admin detail drawer (``/connectors/{source_id}``).
    Returns 404 when the source is not in the DB.
    """
    row = _load_source_row(db, source_id, engagement_id)  # 404 if source not in DB
    return _source_admin_dict(row)


@router.post(
    "/{source_id}/enable",
    response_model=None,
    summary="Enable a configured source — owner/admin gated",
    dependencies=[Depends(require_admin)],
)
def enable_source(
    source_id: str, db: DbSession, engagement_id: EngagementDep
) -> dict[str, Any]:
    """Set ``sources.enabled = true`` and return the updated row (list shape).

    Returns 404 when the source is not in the DB.
    """
    _load_source_row(db, source_id, engagement_id)  # 404 if source not in DB
    db.execute(
        text("UPDATE sources SET enabled = true WHERE source_id = :sid AND engagement_id = :eng"),
        {"sid": source_id, "eng": engagement_id},
    )
    db.flush()
    return _source_admin_dict(_load_source_row(db, source_id, engagement_id))


@router.post(
    "/{source_id}/disable",
    response_model=None,
    summary="Disable a configured source — owner/admin gated",
    dependencies=[Depends(require_admin)],
)
def disable_source(
    source_id: str, db: DbSession, engagement_id: EngagementDep
) -> dict[str, Any]:
    """Set ``sources.enabled = false`` and return the updated row (list shape).

    Returns 404 when the source is not in the DB.
    """
    _load_source_row(db, source_id, engagement_id)  # 404 if source not in DB
    db.execute(
        text("UPDATE sources SET enabled = false WHERE source_id = :sid AND engagement_id = :eng"),
        {"sid": source_id, "eng": engagement_id},
    )
    db.flush()
    return _source_admin_dict(_load_source_row(db, source_id, engagement_id))


@router.post(
    "",
    response_model=SourceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Select / enable a catalog connector (seeds a sources row)",
    dependencies=[Depends(require_admin)],
)
def select_connector(
    body: ConnectorSelectIn,
    db: DbSession,
    engagement_id: EngagementDep,
) -> SourceOut:
    """Enable a connector from the static catalog.

    If the source_id already exists in the DB, it is updated (enabled=True).
    If it does not exist, a new row is seeded from the catalog entry.
    Returns 404 when the source_id is not in the catalog (use POST /connectors/custom
    to add non-catalog sources).
    """
    entry = _CATALOG_INDEX.get(body.source_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"'{body.source_id}' is not in the static catalog. "
                "Use POST /connectors/custom to add a non-catalog source."
            ),
        )

    now = datetime.now(timezone.utc)
    db.execute(
        text("""
            INSERT INTO sources
                (source_id, engagement_id, publisher, url_pattern, auth, class, connector,
                 raw_table, access_method, monthly_budget, quota_ceiling,
                 enabled, notes, created_at)
            VALUES
                (:source_id, :engagement_id, :publisher, :url_pattern, :auth, :class, :connector,
                 :raw_table, :access_method, :monthly_budget, :quota_ceiling,
                 true, :notes, :now)
            ON CONFLICT (source_id) DO UPDATE
                SET enabled        = true,
                    monthly_budget = COALESCE(EXCLUDED.monthly_budget, sources.monthly_budget),
                    quota_ceiling  = COALESCE(EXCLUDED.quota_ceiling,  sources.quota_ceiling),
                    notes          = COALESCE(EXCLUDED.notes, sources.notes)
        """),
        {
            "source_id": body.source_id,
            "engagement_id": engagement_id,
            "publisher": entry["publisher"],
            "url_pattern": entry.get("url_pattern"),
            "auth": entry.get("auth", "none"),
            "class": entry.get("source_class"),
            "connector": entry.get("connector"),
            "raw_table": entry.get("raw_table"),
            "access_method": entry.get("access_method", "api"),
            "monthly_budget": body.monthly_budget,
            "quota_ceiling": body.quota_ceiling,
            "notes": body.notes or entry.get("notes"),
            "now": now,
        },
    )
    db.flush()
    row = _load_source_row(db, body.source_id, engagement_id)
    return SourceOut.model_validate(row)


@router.post(
    "/custom",
    response_model=SourceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a user-defined generic-REST source with an optional field map",
    dependencies=[Depends(require_admin)],
)
def create_custom_source(
    body: CustomSourceIn,
    db: DbSession,
    engagement_id: EngagementDep,
) -> SourceOut:
    """Create (or update) a declarative generic-REST connector source.

    The ``field_map`` is serialised to JSON and stored in ``sources.notes`` as
    ``{\"field_map\": [...], \"user_notes\": \"...\"}`` so the ``generic_rest``
    connector can read it at pull time.

    The connector name is set to ``generic_rest`` and the source is ready for a
    credential write (POST /connectors/{source_id}/credential) if auth != 'none'.
    """
    valid_tables = set(_RAW_TABLE_COLUMNS.keys())
    if body.raw_table not in valid_tables:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"raw_table must be one of: {sorted(valid_tables)}",
        )

    # Serialise the field_map into the notes JSONB.
    notes_payload: dict[str, Any] = {}
    if body.field_map:
        notes_payload["field_map"] = [m.model_dump() for m in body.field_map]
    if body.notes:
        notes_payload["user_notes"] = body.notes
    notes_str = json.dumps(notes_payload) if notes_payload else body.notes

    now = datetime.now(timezone.utc)
    db.execute(
        text("""
            INSERT INTO sources
                (source_id, engagement_id, publisher, url_pattern, auth, class, connector,
                 raw_table, access_method, refresh_cadence, monthly_budget, quota_ceiling,
                 enabled, notes, discovered, created_at)
            VALUES
                (:source_id, :engagement_id, :publisher, :url_pattern, :auth, :class, 'generic_rest',
                 :raw_table, :access_method, :refresh_cadence, :monthly_budget, :quota_ceiling,
                 true, :notes, false, :now)
            ON CONFLICT (source_id) DO UPDATE
                SET publisher        = EXCLUDED.publisher,
                    url_pattern      = EXCLUDED.url_pattern,
                    auth             = EXCLUDED.auth,
                    class            = EXCLUDED.class,
                    raw_table        = EXCLUDED.raw_table,
                    access_method    = EXCLUDED.access_method,
                    refresh_cadence  = COALESCE(EXCLUDED.refresh_cadence, sources.refresh_cadence),
                    notes            = EXCLUDED.notes,
                    monthly_budget   = COALESCE(EXCLUDED.monthly_budget, sources.monthly_budget),
                    quota_ceiling    = COALESCE(EXCLUDED.quota_ceiling,  sources.quota_ceiling)
        """),
        {
            "source_id": body.source_id,
            "engagement_id": engagement_id,
            "publisher": body.publisher,
            "url_pattern": body.url_pattern,
            "auth": body.auth,
            "class": body.source_class,
            "raw_table": body.raw_table,
            "access_method": "api",
            "refresh_cadence": body.refresh_cadence,
            "monthly_budget": body.monthly_budget,
            "quota_ceiling": body.quota_ceiling,
            "notes": notes_str,
            "now": now,
        },
    )
    db.flush()
    row = _load_source_row(db, body.source_id, engagement_id)
    return SourceOut.model_validate(row)


@router.post(
    "/{source_id}/credential",
    response_model=CredentialOut,
    status_code=status.HTTP_201_CREATED,
    summary="Write (or rotate) a connector credential — admin-gated, write-only",
    dependencies=[Depends(require_admin)],
)
def write_credential(
    source_id: str,
    body: CredentialIn,
    db: DbSession,
    engagement_id: EngagementDep,
    user: CurrentUserDep,
) -> CredentialOut:
    """Envelope-encrypt and store a connector API key/token (Q9).

    The secret is encrypted with a fresh per-credential data key (256-bit random),
    and that key is itself wrapped by the ``CRED_MASTER_KEY`` Render secret using
    PostgreSQL's ``pgp_sym_encrypt``. The plaintext never touches disk.

    Calling this when a credential already exists performs a *rotation*: the old
    ciphertext is replaced, ``rotated_at`` is updated, and an audit row is written.

    The endpoint returns ``201`` with the ``cred_ref`` pointer only (never the
    secret). The frontend must never store or display the plaintext.
    """
    # Verify the source exists in this engagement.
    row = _load_source_row(db, source_id, engagement_id)

    action_before = "rotated" if row.get("auth_secret_ref") else "added"

    try:
        cred_ref = credential_service.store(
            db,
            source_id=source_id,
            secret=body.secret,
            actor=user.email or user.id,
        )
    except CredentialServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )

    if body.label:
        db.execute(
            text(
                "UPDATE sources SET notes = COALESCE(notes, '') || :label "
                "WHERE source_id = :sid AND engagement_id = :eng"
            ),
            {"label": f" [key: {body.label}]", "sid": source_id, "eng": engagement_id},
        )

    return CredentialOut(
        cred_ref=cred_ref,
        source_id=source_id,
        action=action_before,
        stored_at=datetime.now(timezone.utc),
    )


@router.delete(
    "/{source_id}/credential",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
    summary="Revoke a connector credential — admin-gated",
    dependencies=[Depends(require_admin)],
)
def revoke_credential(
    source_id: str,
    db: DbSession,
    engagement_id: EngagementDep,
    user: CurrentUserDep,
) -> None:
    """Remove the stored credential for a source and audit the removal.

    Returns 204 regardless of whether a credential existed (idempotent).
    """
    _load_source_row(db, source_id, engagement_id)  # 404 if source not in this engagement
    credential_service.revoke(db, source_id=source_id, actor=user.email or user.id)


@router.post(
    "/{source_id}/probe",
    response_model=ProbeOut,
    summary="Run a connector probe now → 7-state health result",
)
def probe_connector(
    source_id: str,
    db: DbSession,
    engagement_id: EngagementDep,
    _user: CurrentUserDep,
) -> ProbeOut:
    """Execute a live probe for the connector and write back the result.

    Uses the registered connector's ``probe()`` method when available, or falls
    back to a generic HTTP GET to ``url_pattern``. The 7-state taxonomy result is
    persisted to ``sources.last_probe_status / last_probe_at / last_probe_detail``
    so the health dashboard reflects the outcome.
    """
    row = _load_source_row(db, source_id, engagement_id)

    # Decrypt the credential when the source declares auth.
    credential: str | None = None
    cred_ref = row.get("auth_secret_ref")
    if cred_ref:
        credential = credential_service.retrieve(db, cred_ref=cred_ref)

    result = _run_probe(row, credential)
    probed_at = datetime.now(timezone.utc)
    _write_probe_result(db, source_id, result, probed_at, engagement_id)

    return ProbeOut(
        source_id=source_id,
        status=result.status,
        detail=result.detail,
        sample=result.sample,
        probed_at=probed_at,
    )


class PullOut(BaseModel):
    source_id: str
    raw_table: str
    probe_status: str
    rows_landed: int
    rows_normalized: int
    detail: str


def _raw_table_columns(db: Session, raw_table: str) -> set[str]:
    rows = db.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": raw_table},
    ).all()
    return {r[0] for r in rows}


@router.post(
    "/{source_id}/pull",
    response_model=PullOut,
    summary="Pull real data from a configured connector into this engagement's raw layer",
)
def pull_connector(
    source_id: str,
    db: DbSession,
    engagement_id: EngagementDep,
    _user: CurrentUserDep,
) -> PullOut:
    """Ingest live rows from a source the user has configured (endpoint + credential).

    This is the "you gave us an API/URL → we pull your data" step of guided
    connector onboarding: it resolves the connector, probes it, and — when
    healthy — pulls payloads and lands them (deduped by content hash) into the
    source's ``raw_*`` table **stamped with this engagement_id** and with typed
    columns filled from ``normalize()``. The landed rows are immediately drillable
    on the Sources / Cell Detail screens. Re-sizing cells from the new evidence is
    a follow-on (engagement-aware pipeline); this proves and lands the data flow.

    404 if the source isn't in this engagement; 409 if it has no runnable
    connector; 502 with the probe detail if the endpoint isn't reachable/authorised.
    """
    discover()
    row = _load_source_row(db, source_id, engagement_id)

    raw_table = row.get("raw_table")
    if not row.get("connector"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                "This source has no runnable connector. For a proposed source, "
                "set connector='generic_rest' with a base_url + endpoints config, "
                "or use a catalog connector."
            ),
        )
    if raw_table not in _RAW_TABLE_COLUMNS:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail=f"Unknown raw_table {raw_table!r} for this source.")

    credential: str | None = None
    cred_ref = row.get("auth_secret_ref")
    if cred_ref:
        credential = credential_service.retrieve(db, cred_ref=cred_ref)

    connector = get_connector(row, credential)
    if connector is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail=f"No connector implementation resolved for {source_id!r}.")

    # Gate the pull on a healthy probe (SSRF-guarded for generic HTTP).
    try:
        probe = connector.probe()
        pstatus = getattr(probe, "status", "UNREACHABLE")
        pdetail = getattr(probe, "detail", "")
    except Exception as exc:  # noqa: BLE001
        pstatus, pdetail = "UNREACHABLE", str(exc)
    _write_probe_result(db, source_id, ProbeResult(pstatus, pdetail, None),
                        datetime.now(timezone.utc), engagement_id)
    if pstatus != "OK":
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=f"Probe {pstatus}: {pdetail or 'endpoint not reachable/authorised'}.",
        )

    # Engagement-scoped taxonomy + geographies for the connector's pull().
    taxonomy = [dict(r._mapping) for r in db.execute(
        text(
            "SELECT sc.subcategory_id, sc.name, sc.hs_codes, sc.regulatory_codes, "
            "       f.name AS family "
            "FROM taxonomy_subcategories sc JOIN taxonomy_families f USING (family_id) "
            "WHERE sc.engagement_id = :e"
        ), {"e": engagement_id})]
    geographies = [dict(r._mapping) for r in db.execute(
        text("SELECT geography_id, country, segment FROM geographies WHERE engagement_id = :e"),
        {"e": engagement_id})]

    cols = _raw_table_columns(db, raw_table)
    landed = 0
    normalized = 0
    # Bound an interactive pull by wall-clock as well as row count: real
    # connectors (World Bank, Comtrade) iterate many slow calls, so we return
    # promptly with whatever landed and let the user pull again for more.
    import time as _time
    deadline = _time.monotonic() + 25.0
    truncated = False
    try:
        for raw in connector.pull(taxonomy=taxonomy, geographies=geographies, since=None):
            if _time.monotonic() > deadline:
                truncated = True
                break
            payload = json.dumps(raw, default=str, ensure_ascii=False)
            new_row = db.execute(
                text(
                    f"INSERT INTO {raw_table} (source_id, engagement_id, raw_json) "
                    f"SELECT :sid, :eng, CAST(:payload AS jsonb) "
                    f"WHERE NOT EXISTS (SELECT 1 FROM {raw_table} "
                    f"  WHERE source_id = :sid AND engagement_id = :eng "
                    f"  AND md5(raw_json::text) = md5(CAST(:payload AS jsonb)::text)) "
                    f"RETURNING raw_id"
                ),
                {"sid": source_id, "eng": engagement_id, "payload": payload},
            ).first()
            if new_row is None:
                continue
            landed += 1
            # Best-effort typed-column fill from normalize().
            try:
                norm = connector.normalize(raw)
            except Exception:  # noqa: BLE001
                norm = None
            if norm:
                setcols = {k: v for k, v in norm.items() if k in cols and k not in ("raw_id", "source_id", "engagement_id", "raw_json")}
                if setcols:
                    assigns = ", ".join(f"{k} = :{k}" for k in setcols)
                    params = {**setcols, "rid": new_row.raw_id}
                    db.execute(text(f"UPDATE {raw_table} SET {assigns} WHERE raw_id = :rid"), params)
                    normalized += 1
            if landed >= 500:  # bound a single interactive pull
                break
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("pull failed for %s: %s", source_id, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            detail=f"Pull failed: {str(exc)[:200]}") from exc

    more = " (partial — pull again for more)" if truncated else ""
    return PullOut(
        source_id=source_id, raw_table=raw_table, probe_status=pstatus,
        rows_landed=landed, rows_normalized=normalized,
        detail=(f"Landed {landed} new rows ({normalized} typed) into {raw_table}{more}. "
                "Drill any cell using this source to see the raw evidence."
                if landed else "No new rows (endpoint returned nothing new)."),
    )


@router.post(
    "/{source_id}/suggest-mapping",
    response_model=SuggestMappingOut,
    summary="AI-assisted field mapping suggestion for a custom REST source",
)
def suggest_mapping(
    source_id: str,
    body: SuggestMappingIn,
    _user: CurrentUserDep,
) -> SuggestMappingOut:
    """Call the AI mapping service to suggest a field map for a custom source.

    Given a sample payload from the source and the target ``raw_*`` table, Claude
    (via the Anthropic API) returns a declarative field map ready to be saved via
    ``POST /connectors/custom`` or pasted into the UI.

    When ``ANTHROPIC_API_KEY`` is absent, a heuristic name-similarity fallback
    runs instead and clearly marks the result as low-confidence.

    The source does not need to exist in the DB (use this before creating it), but
    providing ``source_id`` lets the response reference the intended target.
    """
    valid_tables = set(_RAW_TABLE_COLUMNS.keys())
    if body.target_table not in valid_tables:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"target_table must be one of: {sorted(valid_tables)}",
        )

    return _call_ai_mapping(source_id, body.sample_payload, body.target_table)


__all__ = ["router"]
