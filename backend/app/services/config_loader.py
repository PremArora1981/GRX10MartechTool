"""Config loader — seed the spine tables from ``config/*.yaml`` + ``players.csv``,
and export them back to YAML/CSV with secrets redacted.

Authority model (v1-definition Q11): the **app/database is the single source of
truth**. YAML files are a *seed* on first boot and a *backup* on export — never a
live mirror. Accordingly:

* ``taxonomy_families`` / ``taxonomy_subcategories`` are **versioned, not
  overwritten** — when seed content differs from what is stored, the row's
  ``version`` counter is bumped (the stable YAML-supplied id is preserved so
  downstream foreign keys never break). Re-running with identical content is a
  no-op (idempotent).
* ``geographies`` and ``method_registry`` are upserted idempotently by primary
  key.
* ``companies`` (from ``players.csv``) are upserted by ``company_id``.
* ``sources`` are upserted on the **seed columns only** — connector-health,
  budget, ``enabled`` and credential pointers managed by the app are never
  clobbered by a re-seed.

Nothing here ever reads or writes ``connector_credentials``; export redacts
everything secret by construction (only the ``auth_secret_ref`` *pointer* — not
any secret material — is round-tripped).

CLI::

    python -m backend.app.services.config_loader load   [CONFIG_DIR]
    python -m backend.app.services.config_loader export [OUT_DIR]

``CONFIG_DIR`` defaults to ``config/``; ``OUT_DIR`` defaults to
``config/_export`` so an export never silently overwrites the hand-maintained
seed files.
"""

from __future__ import annotations

import csv
import logging
import os
import sys
from pathlib import Path
from typing import Any, Iterable

import yaml
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger("grx10.config_loader")

# Repo root = three parents up from this file (backend/app/services/config_loader.py).
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_EXPORT_DIR = REPO_ROOT / "config" / "_export"


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
def _normalize_db_url(url: str) -> str:
    """Coerce a Render/Heroku-style URL to the SQLAlchemy + psycopg3 driver form."""
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def get_engine() -> Engine:
    """Build a SQLAlchemy engine from ``DATABASE_URL`` (psycopg3, sync)."""
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError(
            "DATABASE_URL is not set. Point it at the Render Postgres instance "
            "(or a local Postgres) before running the config loader."
        )
    return create_engine(_normalize_db_url(raw), future=True, pool_pre_ping=True)


# --------------------------------------------------------------------------- #
# YAML / CSV readers
# --------------------------------------------------------------------------- #
def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        logger.warning("config file missing, skipping: %s", path)
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# --------------------------------------------------------------------------- #
# Loaders (one per spine table). Each returns a small stats dict.
# --------------------------------------------------------------------------- #
def _load_taxonomy(conn: Connection, config_dir: Path) -> dict[str, int]:
    """Versioned upsert of families + subcategories.

    Content change => ``version`` bumped; identical content => untouched.
    """
    data = _read_yaml(config_dir / "taxonomy.yaml")
    stats = {"families_inserted": 0, "families_versioned": 0,
             "subcategories_inserted": 0, "subcategories_versioned": 0}

    for family in data.get("families", []):
        fid = int(family["family_id"])
        name = str(family["name"])
        row = conn.execute(
            text("SELECT name FROM taxonomy_families WHERE family_id = :id"),
            {"id": fid},
        ).fetchone()
        if row is None:
            conn.execute(
                text("INSERT INTO taxonomy_families (family_id, name, version) "
                     "VALUES (:id, :name, 1)"),
                {"id": fid, "name": name},
            )
            stats["families_inserted"] += 1
        elif row.name != name:
            conn.execute(
                text("UPDATE taxonomy_families SET name = :name, version = version + 1 "
                     "WHERE family_id = :id"),
                {"id": fid, "name": name},
            )
            stats["families_versioned"] += 1

        for sub in family.get("subcategories", []):
            sid = int(sub["subcategory_id"])
            s_name = str(sub["name"])
            hs = list(sub.get("hs_codes", []) or [])
            reg = list(sub.get("regulatory_codes", []) or [])
            existing = conn.execute(
                text("SELECT name, family_id, hs_codes, regulatory_codes "
                     "FROM taxonomy_subcategories WHERE subcategory_id = :id"),
                {"id": sid},
            ).fetchone()
            if existing is None:
                conn.execute(
                    text("INSERT INTO taxonomy_subcategories "
                         "(subcategory_id, family_id, name, hs_codes, regulatory_codes, version) "
                         "VALUES (:id, :fid, :name, :hs, :reg, 1)"),
                    {"id": sid, "fid": fid, "name": s_name, "hs": hs, "reg": reg},
                )
                stats["subcategories_inserted"] += 1
            else:
                changed = (
                    existing.name != s_name
                    or existing.family_id != fid
                    or list(existing.hs_codes or []) != hs
                    or list(existing.regulatory_codes or []) != reg
                )
                if changed:
                    conn.execute(
                        text("UPDATE taxonomy_subcategories SET name = :name, "
                             "family_id = :fid, hs_codes = :hs, regulatory_codes = :reg, "
                             "version = version + 1 WHERE subcategory_id = :id"),
                        {"id": sid, "fid": fid, "name": s_name, "hs": hs, "reg": reg},
                    )
                    stats["subcategories_versioned"] += 1
    return stats


def _load_geographies(conn: Connection, config_dir: Path) -> dict[str, int]:
    """Idempotent upsert of (country, segment) geography rows by id."""
    data = _read_yaml(config_dir / "geographies.yaml")
    n = 0
    for geo in data.get("geographies", []):
        conn.execute(
            text("INSERT INTO geographies (geography_id, country, segment) "
                 "VALUES (:id, :country, :segment) "
                 "ON CONFLICT (geography_id) DO UPDATE SET "
                 "country = EXCLUDED.country, segment = EXCLUDED.segment"),
            {"id": int(geo["geography_id"]),
             "country": str(geo["country"]),
             "segment": str(geo["segment"])},
        )
        n += 1
    return {"geographies_upserted": n}


def _load_methods(conn: Connection, config_dir: Path) -> dict[str, int]:
    """Idempotent upsert of the method registry by ``method_code``."""
    data = _read_yaml(config_dir / "methods.yaml")
    n = 0
    for m in data.get("methods", []):
        conn.execute(
            text("INSERT INTO method_registry "
                 "(method_code, description, tier, source_class, is_primary_source, "
                 " confidence_cap, required_raw_tables) "
                 "VALUES (:code, :desc, :tier, :sclass, :primary, :cap, :tables) "
                 "ON CONFLICT (method_code) DO UPDATE SET "
                 "description = EXCLUDED.description, tier = EXCLUDED.tier, "
                 "source_class = EXCLUDED.source_class, "
                 "is_primary_source = EXCLUDED.is_primary_source, "
                 "confidence_cap = EXCLUDED.confidence_cap, "
                 "required_raw_tables = EXCLUDED.required_raw_tables"),
            {"code": str(m["method_code"]),
             "desc": m.get("description"),
             "tier": m.get("tier"),
             "sclass": m.get("source_class"),
             "primary": bool(m.get("is_primary_source", False)),
             "cap": m.get("confidence_cap"),
             "tables": list(m.get("required_raw_tables", []) or [])},
        )
        n += 1
    return {"methods_upserted": n}


def _load_companies(conn: Connection, config_dir: Path) -> dict[str, int]:
    """Idempotent upsert of seeded companies from ``players.csv``."""
    path = config_dir / "players.csv"
    if not path.exists():
        logger.warning("players.csv missing, skipping: %s", path)
        return {"companies_upserted": 0}
    n = 0
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            conn.execute(
                text("INSERT INTO companies "
                     "(company_id, name, company_type, country_hq, seeded_role, discovered) "
                     "VALUES (:id, :name, :ctype, :hq, :role, false) "
                     "ON CONFLICT (company_id) DO UPDATE SET "
                     "name = EXCLUDED.name, company_type = EXCLUDED.company_type, "
                     "country_hq = EXCLUDED.country_hq, seeded_role = EXCLUDED.seeded_role"),
                {"id": int(row["company_id"]),
                 "name": row["name"],
                 "ctype": row.get("company_type"),
                 "hq": row.get("country_hq"),
                 "role": row.get("seeded_role")},
            )
            n += 1
    # Keep the SERIAL sequence ahead of the explicitly-seeded ids so app-created
    # companies don't collide.
    conn.execute(text(
        "SELECT setval(pg_get_serial_sequence('companies', 'company_id'), "
        "GREATEST((SELECT COALESCE(MAX(company_id), 1) FROM companies), 1))"
    ))
    return {"companies_upserted": n}


def _load_sources(conn: Connection, config_dir: Path) -> dict[str, int]:
    """Seed ``sources`` (seed columns only — health/budget/enabled left to the app)."""
    data = _read_yaml(config_dir / "sources.yaml")
    n = 0
    for s in data.get("sources", []):
        conn.execute(
            text('INSERT INTO sources '
                 '(source_id, publisher, url_pattern, auth, auth_secret_ref, "class", '
                 ' connector, refresh_cadence, raw_table, access_method, notes) '
                 'VALUES (:id, :pub, :url, :auth, :ref, :class, :conn, :cadence, '
                 '        :raw, :access, :notes) '
                 'ON CONFLICT (source_id) DO UPDATE SET '
                 'publisher = EXCLUDED.publisher, url_pattern = EXCLUDED.url_pattern, '
                 'auth = EXCLUDED.auth, auth_secret_ref = EXCLUDED.auth_secret_ref, '
                 '"class" = EXCLUDED."class", connector = EXCLUDED.connector, '
                 'refresh_cadence = EXCLUDED.refresh_cadence, raw_table = EXCLUDED.raw_table, '
                 'access_method = EXCLUDED.access_method, notes = EXCLUDED.notes'),
            {"id": str(s["source_id"]),
             "pub": s.get("publisher"),
             "url": s.get("url_pattern"),
             "auth": s.get("auth"),
             "ref": s.get("auth_secret_ref"),
             "class": s.get("class"),
             "conn": s.get("connector"),
             "cadence": s.get("refresh_cadence"),
             "raw": s.get("raw_table"),
             "access": s.get("access_method", "api"),
             "notes": s.get("notes")},
        )
        n += 1
    return {"sources_upserted": n}


def load(config_dir: Path | str | None = None) -> dict[str, int]:
    """Seed every spine table from a config directory inside one transaction."""
    config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    logger.info("loading config from %s", config_dir)
    engine = get_engine()
    stats: dict[str, int] = {}
    with engine.begin() as conn:
        # Order matters: families+geographies before methods/sources/companies that
        # may (later) reference them; taxonomy first so subcategory FKs resolve.
        for loader in (_load_taxonomy, _load_geographies, _load_methods,
                       _load_companies, _load_sources):
            stats.update(loader(conn, config_dir))
    logger.info("config load complete: %s", stats)
    return stats


# --------------------------------------------------------------------------- #
# Export (DB -> YAML/CSV, secrets redacted)
# --------------------------------------------------------------------------- #
def _dump_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, sort_keys=False, allow_unicode=True, default_flow_style=False)


def export(out_dir: Path | str | None = None) -> dict[str, str]:
    """Regenerate the seed YAML/CSV from the database (the source of truth).

    Secrets are redacted by construction: ``connector_credentials`` is never read,
    and ``sources`` exports only the ``auth_secret_ref`` *pointer*, never any
    secret material.
    """
    out_dir = Path(out_dir) if out_dir else DEFAULT_EXPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    engine = get_engine()
    written: dict[str, str] = {}

    with engine.connect() as conn:
        # taxonomy
        families: list[dict[str, Any]] = []
        max_ver = 1
        for fam in conn.execute(text(
                "SELECT family_id, name, version FROM taxonomy_families ORDER BY family_id")):
            max_ver = max(max_ver, fam.version or 1)
            subs = []
            for sub in conn.execute(text(
                    "SELECT subcategory_id, name, hs_codes, regulatory_codes "
                    "FROM taxonomy_subcategories WHERE family_id = :fid "
                    "AND superseded_by IS NULL ORDER BY subcategory_id"),
                    {"fid": fam.family_id}):
                subs.append({
                    "subcategory_id": sub.subcategory_id,
                    "name": sub.name,
                    "hs_codes": list(sub.hs_codes or []),
                    "regulatory_codes": list(sub.regulatory_codes or []),
                })
            families.append({"family_id": fam.family_id, "name": fam.name,
                             "subcategories": subs})
        _dump_yaml(out_dir / "taxonomy.yaml", {"version": max_ver, "families": families})
        written["taxonomy"] = str(out_dir / "taxonomy.yaml")

        # geographies
        geos = [{"geography_id": g.geography_id, "country": g.country, "segment": g.segment}
                for g in conn.execute(text(
                    "SELECT geography_id, country, segment FROM geographies ORDER BY geography_id"))]
        _dump_yaml(out_dir / "geographies.yaml", {"version": 1, "geographies": geos})
        written["geographies"] = str(out_dir / "geographies.yaml")

        # methods
        methods: list[dict[str, Any]] = []
        for m in conn.execute(text(
                "SELECT method_code, description, tier, source_class, is_primary_source, "
                "confidence_cap, required_raw_tables FROM method_registry ORDER BY method_code")):
            entry: dict[str, Any] = {
                "method_code": m.method_code,
                "description": m.description,
                "tier": m.tier,
                "source_class": m.source_class,
                "is_primary_source": m.is_primary_source,
                "required_raw_tables": list(m.required_raw_tables or []),
            }
            if m.confidence_cap:
                entry["confidence_cap"] = m.confidence_cap
            methods.append(entry)
        _dump_yaml(out_dir / "methods.yaml", {"version": 1, "methods": methods})
        written["methods"] = str(out_dir / "methods.yaml")

        # sources (secrets redacted — only the pointer is exported)
        sources: list[dict[str, Any]] = []
        for s in conn.execute(text(
                'SELECT source_id, publisher, url_pattern, auth, auth_secret_ref, "class", '
                'connector, refresh_cadence, raw_table, access_method, notes '
                'FROM sources ORDER BY source_id')):
            entry = {
                "source_id": s.source_id,
                "publisher": s.publisher,
                "url_pattern": s.url_pattern,
                "auth": s.auth,
            }
            if s.auth_secret_ref:
                entry["auth_secret_ref"] = s.auth_secret_ref  # pointer only, never the secret
            entry.update({
                "class": s._mapping["class"],
                "connector": s.connector,
                "refresh_cadence": s.refresh_cadence,
                "raw_table": s.raw_table,
                "access_method": s.access_method,
                "notes": s.notes,
            })
            sources.append(entry)
        _dump_yaml(out_dir / "sources.yaml", {"version": 1, "sources": sources})
        written["sources"] = str(out_dir / "sources.yaml")

        # players.csv
        players_path = out_dir / "players.csv"
        with players_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["company_id", "name", "company_type", "country_hq", "seeded_role"])
            for c in conn.execute(text(
                    "SELECT company_id, name, company_type, country_hq, seeded_role "
                    "FROM companies WHERE discovered = false ORDER BY company_id")):
                writer.writerow([c.company_id, c.name, c.company_type or "",
                                 c.country_hq or "", c.seeded_role or ""])
        written["players"] = str(players_path)

    logger.info("config export complete -> %s", out_dir)
    return written


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _usage() -> str:
    return ("usage: python -m backend.app.services.config_loader "
            "load [CONFIG_DIR] | export [OUT_DIR]")


def main(argv: Iterable[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] not in {"load", "export"}:
        print(_usage(), file=sys.stderr)
        return 2

    command, rest = args[0], args[1:]
    target = rest[0] if rest else None
    try:
        if command == "load":
            stats = load(target)
            print("LOAD OK:", stats)
        else:
            written = export(target)
            print("EXPORT OK:", written)
    except Exception as exc:  # noqa: BLE001 — CLI boundary: report and non-zero exit
        logger.exception("config_loader %s failed", command)
        print(f"{command.upper()} FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
