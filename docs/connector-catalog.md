# Connector Catalog — Automated Market Research Tool

Researched June 2026. Feasibility ratings assume a Python connector on Render (PaaS), weekly cron, verbatim JSONB stored in Postgres.

Legend: ✅ genuinely free + programmatic · 💲 paid · ⚠️ uncertain/verify before building · ❌ not feasible for sanctioned programmatic use.
Confidence class: **A** primary structured (qualifies HIGH) · **B** industry/procedural (qualifies MEDIUM) · **C** triangulation support (gap-fill/scaling only).

---

## Raw-layer mapping

The spec defines 8 raw tables. Research surfaced source classes the spec didn't enumerate (procurement, standards, news/press, hiring signals). Proposed extension — add `raw_procurement`, `raw_standards`, `raw_news`, `raw_signals`:

| raw_* table | Source classes landing here |
|---|---|
| `raw_trade_flows` | Comtrade, US Census, USITC DataWeb, Eurostat Comext, WTO, ASEANstats, China GACC, India TRADESTAT |
| `raw_regulatory` | openFDA, FDA AccessGUDID, EU EUDAMED, China NMPA, FCC OET, EU NANDO |
| `raw_procurement` *(new)* | USASpending, SAM.gov, EU TED, India GeM/CPPP, OCDS registry |
| `raw_filings` | SEC EDGAR, EDINET (JP), BSE (IN), UK Companies House, HKEXnews, cninfo (CN) |
| `raw_transcripts` | Roic.ai, API Ninjas, FMP, EarningsCalls |
| `raw_external_metrics` | World Bank, FRED, WHO GHO, Eurostat, OECD, IMF, UN SDG, DOSM, SingStat, India OGD, Vietnam NSO, China NBS |
| `raw_industry_reports` | WSTS, SIA, SEMI, OICA, ACEA; paid: IDC, Euromonitor, S&P; manual-upload: Gartner, GlobalData, Fitch/BMI |
| `raw_patents` | USPTO PatentsView, USPTO ODP, EPO OPS, The Lens, Google Patents BQ |
| `raw_shipments` | OEC BoL, ImportGenius, ImportYeti (all paid); free aggregate stands in via `raw_trade_flows` |
| `raw_standards` *(new)* | JEDEC, SEMI, 3GPP, ETSI |
| `raw_news` *(new)* | GDELT, PR Newswire / Business Wire / GlobeNewswire RSS, SEC 8-K, Crunchbase, OpenCorporates |
| `raw_signals` *(new)* | BLS, USAJOBS, Adzuna, ATS boards (Greenhouse/Lever/Ashby/SmartRecruiters) |

---

## 1. Trade & customs → `raw_trade_flows`

| source_id | class | access | auth | cost | feasibility | notes |
|---|---|---|---|---|---|---|
| `un_comtrade` | A | REST + bulk | free key (preview keyless) | ✅ free (premium for bulk) | EASY | Backbone. ~500 calls/day free quota; `comtradeapicall` pip lib. HS clCode H0–H6 versioning. |
| `us_census_intltrade` | A | REST | free key | ✅ free | EASY | US-only, authoritative. Array-of-arrays JSON. |
| `eurostat_comext` | A | SDMX/REST + bulk CSV | none | ✅ free | MEDIUM | CN8 codes; big — use bulk monthly CSV, async over threshold. |
| `wto_timeseries` | A | REST | free key | ✅ free | EASY | Aggregates + tariffs, weaker at HS6 line detail. |
| `aseanstats` | A | REST (undocumented) | none | ✅ free | MEDIUM | AHTN codes; reverse-engineer indicator codes from portal XHR. |
| `usitc_dataweb` | A | REST | login→bearer | ✅ free | MEDIUM | US tariff depth; complex query JSON. |
| `china_gacc` | B | scrape / 3rd-party | none | ✅ free | HARD | No official API; `chinadata.live` keyless REST as Class-B intermediary. |
| `india_tradestat` | B | scrape | none | ✅ free | HARD | Prefer Comtrade for India; data.gov.in subset is EASY but limited. |

**Cross-cutting trap:** HS-code version normalization (Comtrade H0–H6 vs Eurostat CN8 vs US HTS vs AHTN). Store raw code + classification version in JSONB, normalize to HS6 downstream.

## 2. Regulatory → `raw_regulatory`

| source_id | class | access | auth | cost | feasibility | notes |
|---|---|---|---|---|---|---|
| `openfda` | A | REST + bulk JSON | optional key | ✅ free | EASY | Best-in-class. 240/min, 120k/day keyed. Bulk = full re-dumps. |
| `fda_accessgudid` | A | bulk ZIP + lookup API | none | ✅ free | EASY | Weekly delta ZIP ~1MB; full ~511MB/5.1M records. |
| `eu_eudamed` | A | undocumented REST | none | ✅ free | MEDIUM | Website backend API; fragile, no SLA — build breakage monitor. |
| `cn_nmpa` | A | UDID batch / portal | none | ✅ free | HARD | UDID batch only; JS/captcha portal; Render US IP may be blocked. |
| `fcc_oet_eas` | A | EAS Web API + bulk | none | ✅ free | MEDIUM | ⚠️ Verify EAS Web API contract (KDB 953436) live before building. |
| `eu_nando` | A | scrape | none | ✅ free | MEDIUM | Small, slow; monthly. Inspect new SMCS portal for internal JSON. |

## 3. Procurement → `raw_procurement` (new)

| source_id | class | access | auth | cost | feasibility | notes |
|---|---|---|---|---|---|---|
| `usaspending_gov` | A | REST + bulk | none | ✅ free | EASY | Strongest free procurement source. ~1000 calls/5min. Awards not solicitations. |
| `eu_ted` | A | REST + bulk XML | none | ✅ free | MEDIUM | Cost is eForms/TED XML schema, not access. |
| `ocds_registry` | A | REST/bulk per publisher | varies | ✅ free | EASY–MEDIUM | One generic OCDS ingester → 100+ gov publishers. Highest leverage. |
| `sam_gov` | A | REST | free key | ✅ free | MEDIUM | ⚠️ default **10 req/day** — need role/System account for real ingestion. |
| `india_gem_cppp` | B | scrape | none | ✅ free | HARD | No public API; captcha/session; ToS risk. |

## 4. Filings → `raw_filings`

| source_id | class | access | auth | cost | segments | feasibility | notes |
|---|---|---|---|---|---|---|---|
| `sec_edgar` | A | REST + bulk XBRL | none (UA required) | ✅ free | PDF/raw-XBRL only | EASY (parse HARD) | 10 req/s. Clean JSON = consolidated only; segments need raw XBRL/`num.txt` parse. |
| `edinet_fsa_api_v2` | A | REST + XBRL/CSV | free key | ✅ free | **STRUCTURED (best)** | EASY | Japan. セグメント情報 incl. geographic, dimensionally tagged. Murata/TDK live here. |
| `bse_corporate_filings` | A | undocumented REST + XBRL | none (Origin/Referer) | ✅ free | **STRUCTURED (business)** | MEDIUM | India. Quarterly LODR XBRL <24h; geo split in annual notes. |
| `uk_companies_house` | A | REST + iXBRL docs | free key | ✅ free | effectively NO | EASY–MEDIUM | Registry clean; most filers small/micro = no segments. |
| `hkexnews_filings` | A | undocumented JSON + PDF | none | ✅ free | PDF only | MEDIUM | Discovery easy; segment extraction = PDF parse. |
| `cninfo_cn` | A | undocumented JSON + PDF | none | ✅ free | PDF only | MEDIUM | CN; needs realistic headers + CN/HK proxy from Render. |

**Key finding:** segment/regional revenue is rarely a clean field. Structured only in EDINET, FMP (paid), BSE-XBRL. Everything else = PDF-table parsing. Sequence structured sources first; build one reusable PDF-segment parser before HKEX/cninfo.

## 5. Transcripts → `raw_transcripts`

| source_id | class | access | auth | cost | feasibility | notes |
|---|---|---|---|---|---|---|
| `roic_ai` | B | REST | free key | ✅ free tier | EASY | Best free sanctioned option. 5 req/min, 2-yr history. |
| `api_ninjas` | B | REST | key | 💲 $99/mo to store | EASY | Developer tier forbids storage → need Business for JSONB. |
| `financial_modeling_prep` | B | REST | key | 💲 ~$22–149/mo | EASY | Also the affordable structured-segment backbone (see industry). |
| `earningscalls_dev` | B | REST + MCP | key | 💲 cheap (verify) | EASY | MCP-ready. |
| `seeking_alpha`/`alphasense`/`sentieo` | — | — | — | 💲💲 enterprise / ToS | HARD | Avoid (Sentieo folded into AlphaSense). |

## 6. Macro & national stats → `raw_external_metrics`

| source_id | class | access | auth | cost | feasibility | notes |
|---|---|---|---|---|---|---|
| `world_bank_indicators` | C | REST + bulk | none | ✅ free | EASY | Cleanest. `?format=json`, response `[meta,data]`. |
| `fred_stlouisfed` | C | REST | free key | ✅ free | EASY | 120/min. v1 vs v2 auth — confirm. |
| `who_gho_odata` | C | OData REST | none | ✅ free | EASY | Host on azureedge.net — make base URL configurable. |
| `eurostat_dissemination_api` | C | JSON-stat/SDMX | none | ✅ free | EASY | `pyjstat`; per-query size cap, async holding. |
| `imf_data_sdmx_api` | C | SDMX/REST | none | ✅ free | MEDIUM | ⚠️ old host decommissioned Nov 2025. Start with DataMapper (easy JSON). |
| `oecd_sdmx_api` | C | SDMX | none | ✅ free | MEDIUM | ⚠️ migrated to sdmx.oecd.org — dataflow IDs changed. |
| `un_sdg_indicators` | C | REST + SDMX | none | ✅ free | MEDIUM | Heavy params/pagination. |
| `malaysia_dosm_opendosm` | B | REST + Parquet | none | ✅ free | EASY | Cleanest national agency; read static Parquet for verbatim. |
| `singapore_singstat` | B | REST | none (UA req) | ✅ free | EASY | Descriptive User-Agent mandatory. |
| `india_data_gov_in_ogd` | B | REST | free key | ✅ free | EASY | Per-dataset resource-id discovery; MoSPI partly separate. |
| `vietnam_nso_pxweb` | B | PxWeb REST | none | ✅ free | MEDIUM | ⚠️ verify `/api/v1/` enabled + TLS cert handling. |
| `china_nbs_national_data` | B | undocumented JSON | none | ✅ free | HARD | easyquery.htm; geoblock risk from Render → proxy/intermediary. |

## 7. Industry reports → `raw_industry_reports`

| source_id | class | access | auth | cost | feasibility | notes |
|---|---|---|---|---|---|---|
| `wsts_historical_billings` | B | XLSX file | none | ✅ free tier | EASY | Single stable XLSX; internal-use only, don't redistribute. |
| `sia_global_sales_report` | B | scrape + XLSX | none | ✅ free | MEDIUM | Cloudflare 403 — browser UA; republishes WSTS headline. |
| `semi_equipment_billings` | B | scrape (PR mirror) | none | ✅ free | MEDIUM | Use PR Newswire mirror; detail behind paid EMDS. |
| `oica_vehicle_statistics` | B | scrape + file | none | ✅ free | MEDIUM | Annual, long lag. |
| `acea_vehicle_registrations` | B | scrape/PDF | none | ✅ free | MEDIUM–HARD | Prefer ECB Data Portal dataset `CAR` (proper API) over scraping. |
| `idc_data` | B | REST + SFTP | contract key | 💲 paid | MEDIUM | Real API but subscriber-provisioned. |
| `euromonitor_passport` | B | REST/JSON | contract key | 💲 paid | MEDIUM | Best paid ergonomics; entitlement-gated. |
| `sp_global_market_intelligence` | B | REST + Xpressfeed | contract | 💲 paid | MEDIUM | Per-dataset entitlements. |
| `gartner` / `globaldata` / `fitch_bmi` | B | manual upload | — | 💲 paid | HARD | No realistic API — manual licensed-figure upload / PDF extraction. |

## 8. Patents → `raw_patents`

| source_id | class | access | auth | cost | feasibility | notes |
|---|---|---|---|---|---|---|
| `uspto_patentsview` | C | REST | free key | ✅ free | EASY | Legacy host sunset May 2025 → use search.patentsview.org. ~45/min. |
| `uspto_odp` | C | REST + bulk | free key | ✅ free | EASY–MEDIUM | ⚠️ sign-in change ~Jun 18 2026; bulk 20 dl/file/yr. |
| `epo_ops` | C | REST | OAuth2 | ✅ freemium (~4GB/wk) | MEDIUM | Token ~20min TTL; XML primary; EP/global. |
| `lens_org` | C | REST | bearer | 💲 paid (trial) | EASY | Cleanest global JSON; production needs paid plan. |
| `google_patents_bigquery` | C | BigQuery SQL | GCP creds | 💲 freemium (1TB/mo free) | MEDIUM | Warehouse not feed; backfill/analytics. |
| `wipo_patentscope` | C | UI + bulk XML | reg | ⚠️ free | HARD | No verified free REST API — use EPO/Lens for PCT. |

## 9. Shipments → `raw_shipments`

**Headline:** US bill-of-lading is legally public but has NO free government API. Free trade APIs (Census/Comtrade/USITC) are aggregate only — no shipper/consignee. Shipment-level = pay a vendor.

| source_id | class | access | auth | cost | feasibility | notes |
|---|---|---|---|---|---|---|
| `oec_bill_of_lading` | B | REST (JSON/CSV/Parquet) | key | 💲 $299–1,999/mo | EASY–MEDIUM | Cheapest real BoL; ⚠️ confirm company-level on Pro vs Premium. |
| `importgenius_api` | B | REST | key | 💲 ~$899/user/mo | EASY–MEDIUM | Documented paid BoL API, 24+ jurisdictions. |
| `importyeti_api` | B | REST (beta) | key | ⚠️💲 ~$1k/mo | MEDIUM | Official beta API exists; endpoints/pricing unverified. |
| `panjiva_spglobal` / `datamyne_descartes` | B | contract | contract | 💲💲 enterprise | HARD | Procurement-grade. |
| `cbp_ams_manifest_foia` | A | FOIA (no API) | manual | n/a | ❌ | The real source, but not programmatic. |

## 10. Standards → `raw_standards` (new)

| source_id | class | access | auth | cost | feasibility | notes |
|---|---|---|---|---|---|---|
| `3gpp` | B | FTP/HTTP file server | none | ✅ free | EASY | Work Plan .xlsx + OpenAPI YAML; only truly structured standards source. |
| `etsi` | B | search CSV export + crawl | none | ✅ free | EASY–MEDIUM | 50-result export cap; also hosts 3GPP roster. |
| `jedec` | B | scrape | none | ✅ free | EASY | Member list = single HTML page. |
| `semi` | B | scrape | none/login | ✅ free/partial | MEDIUM | Directory may be JS/gated; SEMIViews paid — don't scrape. |

## 11. News & M&A → `raw_news` (new)

| source_id | class | access | auth | cost | feasibility | notes |
|---|---|---|---|---|---|---|
| `gdelt_doc_api` | C | REST | none | ✅ free | EASY | Standout free news/M&A sweep. 250 rec/call, paginate by datetime. |
| `gdelt_files` | C | bulk CSV.zip 15min | none | ✅ free | MEDIUM | ⚠️ HTTP-only host, validate MD5. |
| `sec_edgar` (8-K) | C | REST | none (UA) | ✅ free | EASY | M&A cross-ref; filter forms=8-K then parse Item. |
| `prnewswire_rss` / `businesswire_rss` / `globenewswire_rss` | C | RSS | none | ✅ free | EASY | One generic RSS connector; headlines only, full body needs page fetch. |
| `crunchbase_api` | C | REST | key | 💲 paid (free tier removed 2025) | EASY | Funding/M&A enrichment. |
| `opencorporates_api` | C | REST | token | ⚠️💲 free by application | EASY–MEDIUM | Public-benefit application, not instant. |

## 12. Hiring/capacity signals → `raw_signals` (new)

| source_id | class | access | auth | cost | feasibility | notes |
|---|---|---|---|---|---|---|
| `bls_publicdata_api_v2` | C | REST POST | free key | ✅ free | EASY | JOLTS; 21-char series IDs; 500 q/day, 50 series/q. |
| `usajobs_search_api` | C | REST GET | free key | ✅ free | EASY | UA=email + Authorization-Key; ≤500/page, ~10k cap. |
| `adzuna` | C | REST | app_id+key | ✅ freemium | EASY | Broad aggregator; verify free quota. |
| ATS family (`greenhouse`,`lever`,`ashby`,`smartrecruiters`) | C | REST GET | none | ✅ free | EASY | One generic connector + companies×ATS slug table. |
| `lightcast` / `thinknum` / `revelio_labs` | C | REST/contract | OAuth/key | 💲 paid→enterprise | MEDIUM–HARD | Structured labor analytics. |
| `linkedin_talent` / `indeed` | — | — | — | — | ❌ | Closed/deprecated — not feasible. |

---

## Recommended build order (cross-class)

**Wave 1 — free, EASY, clean JSON (build first):**
`un_comtrade`, `us_census_intltrade`, `world_bank_indicators`, `fred_stlouisfed`, `who_gho_odata`, `openfda`, `fda_accessgudid`, `usaspending_gov`, `sec_edgar`, `edinet_fsa_api_v2`, `gdelt_doc_api`, `uspto_patentsview`, `bls_publicdata_api_v2`, `ocds_registry` (generic family), newswire RSS trio (generic), ATS family (generic).

**Wave 2 — free but more plumbing (SDMX, OAuth, undocumented, XML):**
`eurostat_comext`, `eurostat_dissemination_api`, `imf_data_sdmx_api` (DataMapper first), `oecd_sdmx_api`, `wto_timeseries`, `eu_ted`, `epo_ops`, `bse_corporate_filings`, `uk_companies_house`, `3gpp`, `etsi`, `malaysia_dosm_opendosm`, `singapore_singstat`, `roic_ai`, `sam_gov` (after account upgrade).

**Wave 3 — scrape/proxy/PDF-heavy (defer behind a reusable PDF parser + proxy):**
`hkexnews_filings`, `cninfo_cn`, `china_gacc`, `china_nbs_national_data`, `cn_nmpa`, `india_gem_cppp`, `india_tradestat`, `eu_eudamed`, `eu_nando`, `sia`/`semi`/`oica`/`acea`, `jedec`/`semi` rosters, `vietnam_nso_pxweb`.

**Wave 4 — paid, only when a contract/budget exists:**
`fmp` (segments), `oec_bill_of_lading`, `importgenius_api`, `lens_org`, `idc_data`, `euromonitor_passport`, `sp_global`, `crunchbase_api`; manual-upload lane for Gartner / GlobalData / Fitch-BMI.

## Method → source feed map (spec's 17 + new)

| method_code | tier | feeds from |
|---|---|---|
| `comtrade_hs4_import` | A | `raw_trade_flows` (Comtrade, Census, Eurostat, USITC) |
| `regulatory_count_unit_price` | A | `raw_regulatory` (openFDA, GUDID, FCC, NMPA) |
| `filings_segment_extraction` | A | `raw_filings` (EDINET structured; EDGAR/BSE; HKEX/cninfo via PDF parse) |
| `tender_award_aggregation` | A | `raw_procurement` (USASpending, TED, OCDS, SAM) |
| `top_down_industry_allocation` | B | `raw_industry_reports` (WSTS, SIA/SEMI, paid publishers) |
| `transcript_mining` | B | `raw_transcripts` (Roic, FMP, API Ninjas) |
| `customs_reconciliation` | B | `raw_trade_flows` + `raw_external_metrics` |
| `activity_volume_unit_price` | B | `raw_external_metrics` (World Bank, national stats) |
| *new* `patent_activity_proxy` | C | `raw_patents` (PatentsView, EPO, Lens) |
| *new* `shipment_aggregation` | C | `raw_shipments` (OEC, ImportGenius) |
| *new* `hiring_capacity_proxy` | C | `raw_signals` (BLS, ATS boards) |
| *new* `news_event_detection` (catalysts) | C | `raw_news` (GDELT, 8-K, newswire) |

## Universal engineering notes

- **HS-code versioning** is the #1 normalization trap — store raw code + version, normalize to HS6.
- **Render egress is a US/EU datacenter IP** — China sources (GACC, NBS, NMPA, cninfo) and bot-protected sites (NSE, ACEA, SIA) likely need a proxy or third-party intermediary.
- **Segment revenue = PDF parsing** outside EDINET/FMP/BSE — build one reusable PDF-segment-note extractor before the PDF-only filing sources.
- **Connector families** (OCDS, ATS boards, newswire RSS, SDMX agencies) = one declarative pattern, many endpoints. Model connectors so a family shares code + a per-endpoint config row.
- **No published rate limits** on most gov APIs (World Bank, OECD, IMF, WHO, Eurostat, TED) — throttle defensively with backoff.
- **Migrated hosts** to avoid in any legacy wrapper: OECD (`stats.oecd.org` retired), IMF (`dataservices.imf.org` decommissioned Nov 2025), PatentsView (`api.patentsview.org` 410), USPTO PEDS (gone Mar 2025).
