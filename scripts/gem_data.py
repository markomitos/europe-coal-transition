"""Import actual coal units from the pinned official GEM GCPT map.

GEM unit/project IDs are retained directly. Ownership, coordinates and source
links belong to units; closure years never become future retirement commitments.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import sources
from scripts.constants import REFERENCE_YEAR
from scripts.geography import COUNTRIES, COUNTRY_ALIASES, normalize_text, stable_id
from scripts.sources import SOURCE_DIR

GEM_SNAPSHOT = "2026-08"
GEM_URL = "https://publicgemdata.nyc3.cdn.digitaloceanspaces.com/interim_maps/gcpt_map_2026-08.geojson"
# Reuse the previously downloaded status snapshot; its bytes already contain units.
GEM_CACHE_FILENAME = "gcpt-status-2026-08.geojson"
STATUS_MAP = {"operating": "Operating", "retired": "Retired", "mothballed": "Mothballed"}


def _years(values: pd.Series) -> pd.Series:
    """Keep only observed, unambiguous years up to the reference year."""
    extracted = values.astype("string").str.extract(r"^\s*(\d{4})(?:\.0)?\s*$", expand=False)
    years = pd.to_numeric(extracted, errors="coerce")
    return years.where(years.between(1900, REFERENCE_YEAR)).astype("Int64")


def clean_gem_units(features: list[dict]) -> pd.DataFrame:
    """Normalize existing European units without aggregating projects or owners."""
    rows = []
    excluded_country = 0
    excluded_status = 0
    for feature in features:
        properties = feature.get("properties") or {}
        iso = COUNTRY_ALIASES.get(normalize_text(properties.get("country-area1", "")))
        if iso is None:
            excluded_country += 1
            continue
        status = str(properties.get("status", "")).lower().strip()
        if status not in STATUS_MAP:
            excluded_status += 1
            continue
        required = ["unit-id", "project-id", "name", "url"]
        if any(not str(properties.get(key) or "").strip() for key in required):
            raise ValueError("GEM unit is missing an identifier, project name or source URL.")
        if properties.get("units-of-m") != "MW":
            raise ValueError("GEM capacity must be reported in MW.")
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates") or []
        point = geometry.get("type") == "Point" and len(coordinates) == 2
        owner = str(properties.get("owner") or "").strip() or "Unknown owner"
        unit_id = str(properties["unit-id"]).strip()
        rows.append({
            "unit_id": unit_id,
            "plant_id": str(properties["project-id"]).strip(),
            "plant_name": str(properties["name"]).strip(),
            "unit_name": str(properties.get("unit-name") or "").strip() or unit_id,
            # Preserve distinct reported groups, including punctuation and stakes.
            "owner_id": hashlib.sha256(owner.encode("utf-8")).hexdigest()[:24],
            "owner_name": owner,
            "country_iso": iso,
            "country_name": COUNTRIES[iso],
            "region_id": stable_id(iso, "Unmapped region"),
            "region_name": "Unmapped region",
            "capacity_mw": properties.get("capacity"),
            "commission_year": properties.get("start-year"),
            "closure_year": properties.get("end-year") if status == "retired" else None,
            "target_retire_year": None,
            "is_retirement_estimated": 1,
            "status": STATUS_MAP[status],
            "latitude": coordinates[1] if point else None,
            "longitude": coordinates[0] if point else None,
            "source_url": str(properties["url"]).strip(),
            "source_snapshot": GEM_SNAPSHOT,
        })
    if not rows:
        raise ValueError("GEM snapshot has no existing units in the configured countries.")
    units = pd.DataFrame(rows)
    units["capacity_mw"] = pd.to_numeric(units["capacity_mw"], errors="coerce")
    if not (np.isfinite(units["capacity_mw"]) & units["capacity_mw"].gt(0)).all():
        raise ValueError("GEM existing units must have finite positive capacity.")
    for column in ["commission_year", "closure_year"]:
        units[column] = _years(units[column])
    if (units["closure_year"] < units["commission_year"]).fillna(False).any():
        raise ValueError("GEM closure year precedes commissioning year.")
    for column, bound in [("latitude", 90), ("longitude", 180)]:
        values = pd.to_numeric(units[column], errors="coerce")
        units[column] = values.where(values.between(-bound, bound))
    before = len(units)
    units = units.drop_duplicates()
    if units["unit_id"].duplicated().any():
        raise ValueError("Conflicting duplicate GEM unit identifiers.")
    projects = units.groupby("plant_id")[["plant_name", "country_iso"]].nunique()
    if projects.gt(1).any().any():
        raise ValueError("Conflicting GEM project names or countries for a project ID.")
    units.attrs["cleaning_report"] = {
        "input_features": len(features), "out_of_scope_features": excluded_country,
        "excluded_status_features": excluded_status,
        "identical_duplicates_removed": before - len(units), "output_units": len(units),
        "projects": int(units["plant_id"].nunique()),
        "missing_commission_year": int(units["commission_year"].isna().sum()),
        "missing_coordinates": int(units[["latitude", "longitude"]].isna().any(axis=1).sum()),
    }
    return units.sort_values(["country_name", "plant_name", "unit_id"]).reset_index(drop=True)


def load_assets(cache_dir: Path = SOURCE_DIR) -> tuple[pd.DataFrame, dict]:
    """Read and validate the official GEM snapshot with its download provenance."""
    payload, provenance = sources.cached_download(GEM_URL, cache_dir / GEM_CACHE_FILENAME)
    data = json.loads(payload)
    if data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
        raise ValueError("GEM source is not a GeoJSON FeatureCollection.")
    units = clean_gem_units(data["features"])
    provenance.update({
        "source_kind": "Official GEM Global Coal Plant Tracker map",
        "record_grain": "unit", "snapshot": GEM_SNAPSHOT,
        "cleaning_report": units.attrs["cleaning_report"],
        "attribution": "Global Energy Monitor; individual source wiki URLs retained per unit",
        "date_policy": "Retired end-year is historical closure; no future commitments imported",
        "ownership_policy": "Raw reported ownership groups; no company/stake allocation inferred",
    })
    return units, provenance
