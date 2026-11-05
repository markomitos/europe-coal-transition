"""Kaggle downloads and regional assignments using GISCO and ONS boundaries.

Raw downloads are runtime-only caches under data/sources, excluded from submission.
Source URLs, SHA-256 hashes, retrieval times, and observation years are recorded.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from scripts.constants import ENERGY_COLUMNS, GENERATION_COLUMNS
from scripts.geography import COUNTRIES, COUNTRY_ALIASES, normalize_text

SOURCE_DIR = Path(__file__).resolve().parents[1] / "data" / "sources"
KAGGLE_DATASETS = {
    "employment": ("rabieelkharoua/employment-rates-by-european-region-1999-2025",
                   "european_regional_employment_quickstart.csv"),
    "gdp": ("rabieelkharoua/regional-gdp-per-person-in-europe-2000-2024", "european_regional_gdp_quickstart.csv"),
    "generation": ("rabieelkharoua/europe-electricity-generation-by-fuel-2008-2026",
                   "europe_electricity_by_source_monthly_quickstart.csv"),
    "energy": ("bhavikjikadara/global-energy-electricity-and-emissions-data", "energy-data.csv"),
}
NUTS_URL = "https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/NUTS_RG_20M_2024_4326_LEVL_2.geojson"
ITL2_URL = (
    "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
    "International_Territorial_Level_2_January_2021_UK_BGC_V2_2022/FeatureServer/0/query?"
    + urlencode({"where": "1=1", "outFields": "ITL221CD,ITL221NM", "outSR": "4326",
                 "returnGeometry": "true", "orderByFields": "ITL221CD",
                 "resultRecordCount": "1000", "geometryPrecision": "6", "f": "geojson"})
)
ITL2_CACHE_FILENAME = "ons-itl2-2021-bgc-v2.geojson"


def load_itl2_boundaries(cache_dir: Path = SOURCE_DIR) -> tuple[list[dict], dict]:
    """Read official UK ITL2 polygons in WGS84, preserving their native TL codes."""
    payload, provenance = cached_download(ITL2_URL, cache_dir / ITL2_CACHE_FILENAME)
    data = json.loads(payload)
    if (not isinstance(data, dict) or data.get("type") != "FeatureCollection"
            or not isinstance(data.get("features"), list)
            or not data["features"]
            or data.get("exceededTransferLimit")
            or (data.get("properties") or {}).get("exceededTransferLimit")):
        raise ValueError("ONS ITL2 source must be a complete GeoJSON FeatureCollection.")
    crs_name = ((data.get("crs") or {}).get("properties") or {}).get("name")
    if crs_name and crs_name not in {"EPSG:4326", "urn:ogc:def:crs:EPSG::4326",
                                     "urn:ogc:def:crs:OGC:1.3:CRS84", "urn:ogc:def:crs:OGC::CRS84"}:
        raise ValueError("ONS ITL2 polygons must use WGS84 longitude/latitude coordinates.")
    regions = []
    seen = set()
    for feature in data["features"]:
        properties = feature.get("properties") or {}
        code = properties.get("ITL221CD")
        name = properties.get("ITL221NM")
        geometry = feature.get("geometry") or {}
        if (not isinstance(code, str) or not code.startswith("TL") or len(code) != 4
                or not isinstance(name, str) or not name.strip()
                or geometry.get("type") not in {"Polygon", "MultiPolygon"}
                or not geometry.get("coordinates")):
            raise ValueError("ONS ITL2 source has an invalid region code, name or polygon.")
        if code in seen:
            raise ValueError("ONS ITL2 source has duplicate region codes.")
        seen.add(code)
        regions.append({"region_id": code, "country_iso": "GB", "region_name": name,
                        "geometry": geometry})
    provenance.update({"source_kind": "ONS International Territorial Level 2 boundaries",
                       "geography_vintage": "2021-01", "geometry_variant": "BGC V2",
                       "coordinate_reference_system": "EPSG:4326", "region_count": len(regions),
                       "catalogue_url": "https://www.arcgis.com/home/item.html?id=6750ae0351c749c4b40b31e5740233a0",
                       "licence_url": "https://www.ons.gov.uk/methodology/geography/licences",
                       "attribution": "Office for National Statistics; contains Ordnance Survey and ONS intellectual property rights"})
    return regions, provenance


def cached_download(url: str, destination: Path) -> tuple[bytes, dict]:
    """Fetch a public HTTPS URL once with bounded size, atomic caching and provenance."""
    if not url.startswith("https://"):
        raise ValueError("Data sources must use HTTPS.")
    cached = destination.exists()
    if cached:
        payload = destination.read_bytes()
        retrieved = datetime.fromtimestamp(destination.stat().st_mtime, timezone.utc).isoformat()
    else:
        try:
            with urlopen(Request(url, headers={"User-Agent": "CoalTransitionTracker/2.0"}), timeout=60) as response:
                if not response.geturl().startswith("https://"):
                    raise ValueError("Source redirected away from HTTPS.")
                payload = response.read(80 * 1024 * 1024 + 1)
        except OSError as exc:
            raise ValueError(
                f"Cannot download {url}: {exc}. Retry, or place the downloaded source archive at {destination}.") from exc
        if len(payload) > 80 * 1024 * 1024:
            raise ValueError("Source exceeds the 80 MiB download limit.")
        # Reject login/error pages instead of caching them as datasets.
        if destination.suffix == ".zip" and not zipfile.is_zipfile(io.BytesIO(payload)):
            raise ValueError(
                f"Kaggle did not return a ZIP archive for {url}. Download the dataset through Kaggle and save it at {destination}.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=destination.parent, prefix="source-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
        retrieved = datetime.now(timezone.utc).isoformat()
    if len(payload) > 80 * 1024 * 1024:
        raise ValueError("Cached source exceeds the download size limit.")
    return payload, {"url": url, "sha256": hashlib.sha256(payload).hexdigest(),
                     "retrieved_at_utc": retrieved, "cached": cached}


def read_kaggle(key: str, cache_dir: Path = SOURCE_DIR) -> tuple[pd.DataFrame, dict]:
    """Read the verified CSV member from one public Kaggle download archive."""
    ref, filename = KAGGLE_DATASETS[key]
    url = "https://www.kaggle.com/api/v1/datasets/download/" + ref
    payload, provenance = cached_download(url, cache_dir / f"{key}.zip")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 160 * 1024 * 1024:
                raise ValueError("Expanded dataset exceeds the 160 MiB limit.")
            if filename not in archive.namelist():
                raise ValueError(
                    f"Dataset format changed: expected {filename}. Update KAGGLE_DATASETS after checking the source.")
            with archive.open(filename) as stream:
                frame = pd.read_csv(stream, low_memory=False)
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise ValueError(f"Cannot read {key} source: {exc}") from exc
    provenance.update({"dataset": ref, "page": f"https://www.kaggle.com/datasets/{ref}", "file": filename,
                       "raw_rows": len(frame)})
    return frame, provenance


def require_columns(frame: pd.DataFrame, columns: list[str], source: str) -> None:
    """Fail with an actionable error when a provider changes its schema."""
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f"{source} is missing columns: {sorted(missing)}")


def _inside_ring(x: float, y: float, ring: list) -> bool:
    """Ray-cast a geographic point against a ring, including the closing edge."""
    inside = False
    previous = ring[-1]
    for current in ring:
        x1, y1 = previous[:2]
        x2, y2 = current[:2]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
        previous = current
    return inside


def _inside_geometry(x: float, y: float, geometry: dict) -> bool:
    """Test Polygon/MultiPolygon membership while respecting interior holes."""
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    for polygon in polygons:
        outer = polygon[0]
        xs, ys = zip(*(point[:2] for point in outer))
        if min(xs) <= x <= max(xs) and min(ys) <= y <= max(ys):
            if _inside_ring(x, y, outer) and not any(_inside_ring(x, y, hole) for hole in polygon[1:]):
                return True
    return False


def attach_regions(assets: pd.DataFrame, cache_dir: Path = SOURCE_DIR) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Join unit coordinates to GISCO NUTS-2 or UK ONS ITL2 polygons.

    No nearest-region or country-centroid fallback is made for unmatched sites.
    GISCO is simplified and ONS is coastline-clipped; some sites stay unmatched.
    """
    payload, gisco_meta = cached_download(NUTS_URL, cache_dir / "nuts2-2024.geojson")
    features = json.loads(payload)["features"]
    regions = []
    by_country = {}
    provenance = {"gisco": gisco_meta}
    for feature in features:
        properties = feature["properties"]
        iso = {"EL": "GR", "UK": "GB"}.get(properties["CNTR_CODE"], properties["CNTR_CODE"])
        if iso not in COUNTRIES or iso == "GB":
            continue
        region = {"region_id": properties["NUTS_ID"], "country_iso": iso,
                  "region_name": properties.get("NAME_LATN", properties["NUTS_NAME"])}
        regions.append(region)
        by_country.setdefault(iso, []).append({**region, "geometry": feature["geometry"]})
    if "GB" in COUNTRIES:
        uk_regions, provenance["ons_itl2"] = load_itl2_boundaries(cache_dir)
        regions.extend({key: value for key, value in region.items() if key != "geometry"}
                       for region in uk_regions)
        by_country["GB"] = uk_regions
    result = assets.copy()
    for index, row in result.iterrows():
        if pd.isna(row["latitude"]) or pd.isna(row["longitude"]):
            continue
        for feature in by_country.get(row["country_iso"], []):
            if _inside_geometry(float(row["longitude"]), float(row["latitude"]), feature["geometry"]):
                result.loc[index, "region_id"] = feature["region_id"]
                result.loc[index, "region_name"] = feature["region_name"]
                break
    all_regions = pd.concat(
        [pd.DataFrame(regions), result[["region_id", "country_iso", "region_name"]]]).drop_duplicates("region_id")
    provenance["unmatched_asset_records"] = int((result["region_name"] == "Unmapped region").sum())
    provenance["unmatched_uk_asset_records"] = int(
        (result["country_iso"].eq("GB") & result["region_name"].eq("Unmapped region")).sum())
    return result, all_regions, provenance


def load_regional_indicators(regions: pd.DataFrame, year: int, cache_dir: Path = SOURCE_DIR) -> tuple[
    pd.DataFrame, dict]:
    """Join same-year employment and GDP by exact source region code.

    Employment is percent of residents aged 20–64 in employment, not mining jobs.
    The GDP index is the source's euro-per-person EU27 index, not a PPS index.
    ITL2 codes stay distinct; no NUTS/ITL crosswalk or economic fallback is inferred.
    """
    employment, employment_meta = read_kaggle("employment", cache_dir)
    gdp, gdp_meta = read_kaggle("gdp", cache_dir)
    require_columns(employment, ["country_code", "year", "employment_rate_20_64"], "Regional employment")
    require_columns(gdp, ["country_code", "year", "gdp_eur_per_person", "gdp_per_person_eu_index"], "Regional GDP")
    employment = employment.loc[employment["year"] == year, ["country_code", "employment_rate_20_64"]]
    gdp = gdp.loc[gdp["year"] == year, ["country_code", "gdp_eur_per_person", "gdp_per_person_eu_index"]]
    if employment["country_code"].duplicated().any() or gdp["country_code"].duplicated().any():
        raise ValueError("Duplicate regional observations for the requested year.")
    result = regions.merge(employment.rename(columns={"country_code": "region_id"}), on="region_id", how="left")
    result = result.merge(gdp.rename(columns={"country_code": "region_id", "gdp_eur_per_person": "gdp_per_capita_eur",
                                              "gdp_per_person_eu_index": "gdp_index"}), on="region_id", how="left")
    for column in ["employment_rate_20_64", "gdp_per_capita_eur", "gdp_index"]:
        result[column] = pd.to_numeric(result[column], errors="coerce").replace([np.inf, -np.inf], np.nan)
    result.loc[~result["employment_rate_20_64"].between(0, 100), "employment_rate_20_64"] = np.nan
    return result, {"employment": employment_meta, "gdp": gdp_meta}


def load_country_data(cache_dir: Path = SOURCE_DIR) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Load annual macro indicators and monthly fuel generation, without filling gaps."""
    energy, energy_meta = read_kaggle("energy", cache_dir)
    require_columns(energy, ["country", "year", *ENERGY_COLUMNS], "Country energy")
    energy["country_iso"] = energy["country"].map(lambda name: COUNTRY_ALIASES.get(normalize_text(name)))
    energy = energy.loc[energy["country_iso"].notna(), ["country_iso", "year", *ENERGY_COLUMNS]].copy()
    generation, generation_meta = read_kaggle("generation", cache_dir)
    require_columns(generation, ["country_iso2", "period", *GENERATION_COLUMNS], "Monthly generation")
    generation = generation.rename(columns={"country_iso2": "country_iso"})
    generation = generation.loc[
        generation["country_iso"].isin(COUNTRIES), ["country_iso", "period", *GENERATION_COLUMNS]].copy()
    for frame, keys in [(energy, ["country_iso", "year"]), (generation, ["country_iso", "period"])]:
        if frame.duplicated(keys).any():
            raise ValueError(f"Duplicate country observation keys: {keys}")
        for column in frame.columns.difference(keys):
            frame[column] = pd.to_numeric(frame[column], errors="coerce").replace([np.inf, -np.inf], np.nan)
    return energy, generation, {"energy": energy_meta, "generation": generation_meta}
