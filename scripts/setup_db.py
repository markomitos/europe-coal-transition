"""Create and atomically populate the normalized runtime SQLite database.

Run ``python scripts/setup_db.py`` or call ``setup_database`` from Streamlit.
No downloaded data or database is required in the source-only submission.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import pandas as pd

# Support both `python scripts/setup_db.py` and `python -m scripts.setup_db`.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.constants import (
    COAL_CAPACITY_FACTOR,
    ENERGY_COLUMNS,
    GENERATION_COLUMNS,
    OBSERVATION_YEAR,
    REFERENCE_YEAR,
)
from scripts.gem_data import GEM_SNAPSHOT, load_assets
from scripts.geography import COUNTRIES, EU_COUNTRIES
from scripts.sources import (
    ITL2_URL,
    NUTS_URL,
    SOURCE_DIR,
    attach_regions,
    load_country_data,
    load_regional_indicators,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT / "data" / "coal_transition.sqlite"
SCHEMA_VERSION = 7

DDL = f"""
CREATE TABLE dim_countries (
    country_iso TEXT PRIMARY KEY,
    country_name TEXT NOT NULL UNIQUE,
    region_bloc TEXT NOT NULL CHECK(region_bloc IN ('EU', 'Non-EU'))
);
CREATE TABLE dim_regions (
    region_id TEXT PRIMARY KEY,
    country_iso TEXT NOT NULL REFERENCES dim_countries(country_iso),
    region_name TEXT NOT NULL,
    gdp_per_capita_eur REAL CHECK(gdp_per_capita_eur >= 0),
    employment_rate_20_64 REAL CHECK(employment_rate_20_64 BETWEEN 0 AND 100),
    gdp_index REAL CHECK(gdp_index >= 0),
    UNIQUE(country_iso, region_name)
);
CREATE TABLE dim_owners (
    owner_id TEXT PRIMARY KEY,
    owner_name TEXT NOT NULL UNIQUE
);
CREATE TABLE dim_plants (
    plant_id TEXT PRIMARY KEY,
    plant_name TEXT NOT NULL CHECK(plant_name != '')
);
CREATE TABLE fact_units (
    unit_id TEXT PRIMARY KEY,
    plant_id TEXT NOT NULL REFERENCES dim_plants(plant_id),
    owner_id TEXT NOT NULL REFERENCES dim_owners(owner_id),
    region_id TEXT NOT NULL REFERENCES dim_regions(region_id),
    unit_name TEXT NOT NULL CHECK(unit_name != ''),
    capacity_mw REAL NOT NULL CHECK(capacity_mw > 0),
    commission_year INTEGER CHECK(commission_year BETWEEN 1900 AND {REFERENCE_YEAR}),
    closure_year INTEGER CHECK(closure_year BETWEEN 1900 AND {REFERENCE_YEAR}),
    target_retire_year INTEGER CHECK(target_retire_year BETWEEN 1900 AND 2150),
    status TEXT NOT NULL CHECK(status IN ('Operating','Retired','Mothballed')),
    is_retirement_estimated INTEGER NOT NULL CHECK(is_retirement_estimated IN (0,1)),
    latitude REAL CHECK(latitude BETWEEN -90 AND 90),
    longitude REAL CHECK(longitude BETWEEN -180 AND 180),
    source_url TEXT NOT NULL,
    source_snapshot TEXT NOT NULL,
    CHECK(closure_year IS NULL OR status = 'Retired'),
    CHECK(closure_year IS NULL OR commission_year IS NULL OR closure_year >= commission_year),
    CHECK(target_retire_year IS NULL OR commission_year IS NULL OR target_retire_year >= commission_year)
);
CREATE TABLE fact_country_energy (
    country_iso TEXT NOT NULL REFERENCES dim_countries(country_iso),
    year INTEGER NOT NULL,
    population REAL,
    gdp REAL,
    coal_electricity REAL,
    electricity_generation REAL,
    coal_share_elec REAL,
    renewables_electricity REAL,
    renewables_share_elec REAL,
    solar_electricity REAL,
    wind_electricity REAL,
    greenhouse_gas_emissions REAL,
    carbon_intensity_elec REAL,
    PRIMARY KEY(country_iso, year)
);
CREATE TABLE fact_monthly_generation (
    country_iso TEXT NOT NULL REFERENCES dim_countries(country_iso),
    period TEXT NOT NULL,
    total_gwh REAL,
    coal_gwh REAL,
    natural_gas_gwh REAL,
    oil_gwh REAL,
    nuclear_gwh REAL,
    hydro_gwh REAL,
    wind_gwh REAL,
    solar_gwh REAL,
    renewables_gwh REAL,
    PRIMARY KEY(country_iso, period)
);
CREATE TABLE dataset_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX ix_units_plant ON fact_units(plant_id);
CREATE INDEX ix_units_status_retirement ON fact_units(status, target_retire_year);
CREATE INDEX ix_units_region ON fact_units(region_id);
CREATE INDEX ix_units_owner ON fact_units(owner_id);
CREATE INDEX ix_regions_country ON dim_regions(country_iso);
"""


def read_metadata(db_path: str | Path = DEFAULT_DB_PATH) -> dict:
    """Read decoded provenance from an existing database without creating a file."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        return {key: json.loads(value) for key, value in connection.execute(
            "SELECT key, value FROM dataset_metadata")}


def _insert_frame(
        connection: sqlite3.Connection, table: str, frame: pd.DataFrame, columns: list[str],
) -> None:
    """Insert selected columns, converting pandas missing values to SQL NULL.

    Table and column names are internal schema identifiers, never user input.
    """
    selected = frame[columns]
    payload = selected.astype(object).where(selected.notna(), None)
    column_names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    connection.executemany(
        f"INSERT INTO {table} ({column_names}) VALUES ({placeholders})",
        payload.itertuples(index=False, name=None),
    )


def setup_database(
        db_path: str | Path = DEFAULT_DB_PATH, *, force: bool = False,
        source_dir: str | Path = SOURCE_DIR, regional_year: int = OBSERVATION_YEAR,
) -> Path:
    """Build a validated database via a sibling temporary file and atomic replace.

    Reuse a matching database; rebuild old schemas, country scopes or boundaries.
    Failed imports leave the previous database intact. FK enforcement and integrity
    checks run before replacement. Runtime data is excluded from submissions.
    """
    destination = Path(db_path).resolve()
    source_settings = {
        "configured_source": "gem-gcpt-map",
        "asset_snapshot": GEM_SNAPSHOT,
        "regional_year": regional_year,
        "country_scope": sorted(COUNTRIES),
        "boundary_sources": {"gisco": NUTS_URL, "ons_itl2": ITL2_URL},
    }
    if destination.exists() and not force:
        with closing(sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            metadata = read_metadata(destination)
            if metadata.get("real_data_only") is not True:
                raise ValueError("Database does not have verified source provenance. Rebuild with --force.")
            if any(metadata.get(key) != value for key, value in source_settings.items()
                   if key not in {"country_scope", "boundary_sources"}):
                raise ValueError("Source settings changed. Rebuild with python scripts/setup_db.py --force.")
            if all(metadata.get(key) == source_settings[key]
                   for key in ["country_scope", "boundary_sources"]):
                return destination
            # Rebuild old country scopes or boundary assignments before reusing data.
        # Replace old schemas only after a validated build with the current source and groups.
    cache = Path(source_dir)
    units, provenance = load_assets(cache)
    units, region_keys, geometry_meta = attach_regions(units, cache)
    regions, regional_meta = load_regional_indicators(region_keys, regional_year, cache)
    energy, generation, country_meta = load_country_data(cache)
    provenance.update({"real_data_only": True, **source_settings,
                       "sources": {"assets": dict(provenance), "region_boundaries": geometry_meta,
                                   **regional_meta, **country_meta}})
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="coal-bootstrap-", suffix=".sqlite", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with closing(sqlite3.connect(temporary)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(DDL)
            with connection:
                country_rows = []
                for iso, name in COUNTRIES.items():
                    bloc = "EU" if iso in EU_COUNTRIES else "Non-EU"
                    country_rows.append((iso, name, bloc))
                connection.executemany(
                    "INSERT INTO dim_countries (country_iso, country_name, region_bloc) VALUES (?,?,?)",
                    country_rows,
                )
                region_columns = ["region_id", "country_iso", "region_name", "gdp_per_capita_eur",
                                  "employment_rate_20_64", "gdp_index"]
                _insert_frame(connection, "dim_regions", regions, region_columns)
                owners = units[["owner_id", "owner_name"]].drop_duplicates()
                _insert_frame(connection, "dim_owners", owners, ["owner_id", "owner_name"])
                plants = units[["plant_id", "plant_name"]].drop_duplicates()
                _insert_frame(connection, "dim_plants", plants, ["plant_id", "plant_name"])
                columns = [
                    "unit_id", "plant_id", "owner_id", "region_id", "unit_name", "capacity_mw",
                    "commission_year", "closure_year", "target_retire_year", "status",
                    "is_retirement_estimated", "latitude", "longitude", "source_url", "source_snapshot",
                ]
                _insert_frame(connection, "fact_units", units, columns)
                for frame, table, columns in [(energy, "fact_country_energy", ["country_iso", "year", *ENERGY_COLUMNS]),
                                              (generation, "fact_monthly_generation",
                                               ["country_iso", "period", *GENERATION_COLUMNS])]:
                    _insert_frame(connection, table, frame, columns)
                provenance.update({"schema_version": SCHEMA_VERSION, "reference_year": REFERENCE_YEAR,
                                   "capacity_factor": COAL_CAPACITY_FACTOR,
                                   "regional_indicator_source": "Eurostat regional observations via Kaggle",
                                   "coordinate_policy": "Reported source coordinates only; never imputed",
                                   "excluded_asset_fields": ["fuel_type", "technology", "annual_co2_mt",
                                                             "ownership_type"],
                                   "excluded_regional_fields": ["direct_coal_jobs", "population", "coal_share"]})
                connection.executemany("INSERT INTO dataset_metadata (key, value) VALUES (?,?)",
                                       [(key, json.dumps(value, ensure_ascii=False)) for key, value in
                                        provenance.items()])
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Database foreign-key validation failed.")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite integrity check failed.")
            connection.execute("ANALYZE")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def main() -> None:
    """CLI with actionable errors and an explicit rebuild option."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR, help="Runtime source archive cache")
    parser.add_argument("--regional-year", type=int, default=OBSERVATION_YEAR)
    parser.add_argument("--force", action="store_true", help="Atomically rebuild an existing database")
    args = parser.parse_args()
    try:
        path = setup_database(args.db, force=args.force, source_dir=args.source_dir,
                              regional_year=args.regional_year)
        print(f"Database ready: {path}")
        print(json.dumps(read_metadata(path), indent=2, ensure_ascii=False))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Setup failed: {exc}\n")


if __name__ == "__main__":
    main()
