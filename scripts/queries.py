"""Parameterized relational analytics and a bounded, read-only SQL teaching console.

Filters are bound values; SQL strings never interpolate user-provided content.
Scenario retirement overrides live in a connection-local temporary table only.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from scripts.constants import GENERATION_COLUMNS, HIGH_URGENCY_YEAR, REFERENCE_YEAR

COUNTRY_FILTER = "(:all_countries OR c.country_name IN (SELECT value FROM json_each(:countries)))"
FILTER_SQL = COUNTRY_FILTER + """
AND (:all_statuses OR u.status IN (SELECT value FROM json_each(:statuses)))
"""

UNIT_SQL = """
SELECT u.*, p.plant_name, r.region_name, r.gdp_per_capita_eur,
       r.employment_rate_20_64, r.gdp_index, o.owner_name,
       c.country_iso, c.country_name, c.region_bloc,
       (c.region_bloc = 'EU') AS in_eu,
       :reference_year - u.commission_year AS plant_age
FROM fact_units u
JOIN dim_plants p ON p.plant_id = u.plant_id
JOIN dim_regions r ON r.region_id = u.region_id
JOIN dim_countries c ON c.country_iso = r.country_iso
JOIN dim_owners o ON o.owner_id = u.owner_id
WHERE """ + FILTER_SQL + " ORDER BY c.country_name, p.plant_name, u.unit_name"

REGIONAL_SQL = """
SELECT r.region_id, r.region_name, c.country_name, c.region_bloc,
       (c.region_bloc = 'EU') AS in_eu,
       r.gdp_per_capita_eur,
       r.employment_rate_20_64, r.gdp_index,
       SUM(u.capacity_mw) AS total_capacity_mw,
       SUM(CASE WHEN u.status = 'Operating' THEN u.capacity_mw ELSE 0 END) AS operating_mw,
       SUM(CASE WHEN u.status = 'Retired' THEN u.capacity_mw ELSE 0 END) AS retired_mw,
       SUM(CASE WHEN u.status = 'Mothballed' THEN u.capacity_mw ELSE 0 END) AS mothballed_mw,
       AVG(CASE WHEN u.status != 'Retired' THEN :reference_year - u.commission_year END) AS average_age,
       SUM(CASE WHEN u.status != 'Retired' AND u.target_retire_year <= :horizon THEN u.capacity_mw ELSE 0 END) AS retiring_mw
FROM fact_units u
JOIN dim_regions r ON r.region_id = u.region_id
JOIN dim_countries c ON c.country_iso = r.country_iso
WHERE """ + FILTER_SQL + """
GROUP BY r.region_id, r.region_name, c.country_name, c.region_bloc,
         r.gdp_per_capita_eur,
         r.employment_rate_20_64, r.gdp_index
HAVING SUM(u.capacity_mw) > :minimum_capacity
ORDER BY operating_mw DESC, r.region_name
"""

OWNER_SQL = """
WITH totals AS (
    SELECT c.country_name, o.owner_id, o.owner_name,
           SUM(u.capacity_mw) AS capacity_mw
    FROM fact_units u
    JOIN dim_owners o ON o.owner_id = u.owner_id
    JOIN dim_regions r ON r.region_id = u.region_id
    JOIN dim_countries c ON c.country_iso = r.country_iso
    WHERE """ + FILTER_SQL + """ AND u.status != 'Retired'
    GROUP BY c.country_name, o.owner_id, o.owner_name
), shares AS (
    SELECT *, 100.0 * capacity_mw / SUM(capacity_mw) OVER (PARTITION BY country_name) AS national_capacity_pct,
           ROW_NUMBER() OVER (PARTITION BY country_name ORDER BY capacity_mw DESC, owner_name) AS capacity_rank
    FROM totals
)
SELECT * FROM shares WHERE capacity_rank <= :top_n
ORDER BY country_name, capacity_rank
"""

TIMELINE_SQL = """
WITH RECURSIVE years(year) AS (
    SELECT :start_year
    UNION ALL SELECT year + 1 FROM years WHERE year < :end_year
), selected_countries AS (
    SELECT c.country_name, c.region_bloc
    FROM dim_countries c
    WHERE """ + COUNTRY_FILTER + """
), retirements AS (
    SELECT c.country_name,
           MAX(:start_year, u.target_retire_year) AS year,
           SUM(u.capacity_mw) AS retiring_mw,
           SUM(CASE WHEN u.is_retirement_estimated = 1 THEN u.capacity_mw ELSE 0 END) AS estimated_retiring_mw
    FROM fact_units u
    JOIN dim_regions r ON r.region_id = u.region_id
    JOIN dim_countries c ON c.country_iso = r.country_iso
    WHERE """ + FILTER_SQL + """
      AND u.status != 'Retired' AND u.target_retire_year IS NOT NULL
      AND u.target_retire_year <= :end_year
    GROUP BY c.country_name, MAX(:start_year, u.target_retire_year)
), annual AS (
    SELECT years.year, c.country_name, c.region_bloc,
           COALESCE(r.retiring_mw,0) AS retiring_mw,
           COALESCE(r.estimated_retiring_mw,0) AS estimated_retiring_mw
    FROM years CROSS JOIN selected_countries c
    LEFT JOIN retirements r ON r.year = years.year AND r.country_name = c.country_name
)
SELECT *, SUM(retiring_mw) OVER (
    PARTITION BY country_name ORDER BY year ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
) AS cumulative_retiring_mw,
SUM(estimated_retiring_mw) OVER (
    PARTITION BY country_name ORDER BY year ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
) AS cumulative_estimated_mw
FROM annual ORDER BY country_name, year
"""

ENERGY_SQL = """
SELECT c.country_name, c.region_bloc, e.*
FROM fact_country_energy e
JOIN dim_countries c ON c.country_iso = e.country_iso
WHERE """ + COUNTRY_FILTER + """
  AND e.year BETWEEN :start_year AND :end_year
ORDER BY c.country_name, e.year
"""
GENERATION_SQL = """
SELECT c.country_name, c.region_bloc, g.country_iso,
       CAST(SUBSTR(g.period, 1, 4) AS INTEGER) AS year,
       COUNT(*) AS reported_months,
       """ + ",\n       ".join(
    f"CASE WHEN COUNT(g.{field}) = 12 THEN SUM(g.{field}) END AS {field}"
    for field in GENERATION_COLUMNS
) + """
FROM fact_monthly_generation g
JOIN dim_countries c ON c.country_iso = g.country_iso
WHERE """ + COUNTRY_FILTER + """
  AND CAST(SUBSTR(g.period, 1, 4) AS INTEGER) BETWEEN :start_year AND :end_year
GROUP BY c.country_iso, c.country_name, c.region_bloc, CAST(SUBSTR(g.period, 1, 4) AS INTEGER)
ORDER BY c.country_name, year
"""

QUERY_CATALOG = {
    "Regional capacity & exposure": REGIONAL_SQL,
    "Top ownership groups per country": OWNER_SQL,
    "Cumulative phase-out timeline": TIMELINE_SQL,
    "Joined unit records": UNIT_SQL,
    "Country energy observations": ENERGY_SQL,
    "Annual generation with month coverage": GENERATION_SQL,
}


def query_parameters(
        countries: Sequence[str] | None = None, statuses: Sequence[str] | None = None,
        **extra: Any,
) -> dict[str, Any]:
    """Return named SQL bindings; None selects all while an empty list selects none."""
    return {"countries": json.dumps(list(countries or [])), "all_countries": int(countries is None),
            "statuses": json.dumps(list(statuses or [])), "all_statuses": int(statuses is None),
            "reference_year": REFERENCE_YEAR, "horizon": HIGH_URGENCY_YEAR, "minimum_capacity": 100,
            "top_n": 5, "start_year": REFERENCE_YEAR, "end_year": 2040, **extra}


@contextmanager
def database_connection(
        db_path: str | Path, retirement_overrides: Mapping[str, int | None] | None = None,
) -> Iterator[sqlite3.Connection]:
    """Open main DB read-only; optionally shadow unit data in temporary memory.

    No scenario update reaches the persistent dataset. The source DB must exist.
    """
    connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA temp_store = MEMORY")
        if retirement_overrides is not None:
            connection.execute("CREATE TEMP TABLE fact_units AS SELECT * FROM main.fact_units")
            connection.execute("CREATE UNIQUE INDEX temp.ix_scenario_unit ON fact_units(unit_id)")
            connection.executemany("UPDATE temp.fact_units SET target_retire_year = ? WHERE unit_id = ?",
                                   [(int(year) if year is not None else None, unit_id)
                                    for unit_id, year in retirement_overrides.items()])
        yield connection
    finally:
        connection.close()


def _read(
        db_path: str | Path, sql: str, params: dict,
        retirement_overrides: Mapping[str, int | None] | None = None,
) -> pd.DataFrame:
    """Execute a trusted parameterized analytical query and return its data frame."""
    with database_connection(db_path, retirement_overrides) as connection:
        return pd.read_sql_query(sql, connection, params=params)


def get_units(
        db_path: str | Path, countries: Sequence[str] | None = None,
        statuses: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Return joined unit records for the chosen countries and source statuses."""
    return _read(db_path, UNIT_SQL, query_parameters(countries, statuses))


def get_regional_capacity_summary(
        db_path: str | Path, countries: Sequence[str] | None = None,
        statuses: Sequence[str] | None = None, horizon: int = HIGH_URGENCY_YEAR,
        retirement_overrides: Mapping[str, int | None] | None = None,
        minimum_capacity: float = 100,
) -> pd.DataFrame:
    """JOIN region/country/unit tables; aggregate assets with HAVING capacity >100 MW.

    Unit coordinates determine region membership independently within a project.
    Status is reported by the pinned GEM snapshot, not a live operational check.
    """
    return _read(db_path, REGIONAL_SQL, query_parameters(countries, statuses, horizon=horizon,
                                                         minimum_capacity=minimum_capacity), retirement_overrides)


def get_owner_capacity_share(
        db_path: str | Path, countries: Sequence[str] | None = None,
        statuses: Sequence[str] | None = None, top_n: int = 5,
) -> pd.DataFrame:
    """Rank non-retired source capacity within each selected country.

    Raw ownership groups are ranked without splitting or allocating company stakes.
    National capacity shares are computed before the top-N cut.
    """
    if not 1 <= top_n <= 100:
        raise ValueError("top_n must be between 1 and 100.")
    return _read(db_path, OWNER_SQL, query_parameters(countries, statuses, top_n=top_n))


def get_phaseout_timeline(
        db_path: str | Path, start_year: int = REFERENCE_YEAR, end_year: int = 2040,
        countries: Sequence[str] | None = None, statuses: Sequence[str] | None = None,
        retirement_overrides: Mapping[str, int | None] | None = None,
) -> pd.DataFrame:
    """Return a dense annual panel and country window-summed scenario retirements.

    Retired units are excluded. Active assets overdue before start_year are
    counted at start_year as a backlog, rather than treated as historically shut.
    Selected countries have explicit zeroes in both EU and Non-EU groups.
    Estimated MW are exposed separately.
    """
    if not 1900 <= start_year <= end_year <= 2150 or end_year - start_year > 150:
        raise ValueError("Timeline must be ordered, within 1900–2150, and at most 150 years.")
    return _read(db_path, TIMELINE_SQL, query_parameters(countries, statuses,
                                                         start_year=start_year, end_year=end_year),
                 retirement_overrides)


def run_readonly_query(
        db_path: str | Path, sql: str, params: Mapping[str, Any] | None = None,
        retirement_overrides: Mapping[str, int | None] | None = None,
        max_rows: int = 2000, timeout_seconds: float = 2.0,
) -> pd.DataFrame:
    """Run one SELECT/WITH statement with SQLite authorization and resource bounds.

    Main DB is opened read-only; query_only also protects the temporary scenario.
    The authorizer denies writes, attachment, pragmas, and non-whitelisted functions.
    Result rows, SQL size, cell size, VM work, and elapsed time are bounded. A
    recursive query cannot run indefinitely. Returns attrs['truncated'] if capped.
    """
    if not isinstance(sql, str) or len(sql.encode()) > 100_000:
        raise ValueError("SQL must be text under 100 KB.")
    if not 1 <= max_rows <= 10000 or not 0 < timeout_seconds <= 10:
        raise ValueError("Invalid SQL runner row/time limit.")
    allowed_functions = {"sum", "avg", "min", "max", "count", "group_concat", "coalesce", "nullif", "round",
                         "abs", "lower", "upper", "length", "substr", "substring", "trim",
                         "row_number", "rank", "dense_rank", "lag", "lead", "first_value",
                         "last_value", "ifnull", "printf", "json_extract", "json_valid", "typeof"}
    # Initialize eponymous JSON virtual table before the restricted authorizer.
    with database_connection(db_path, retirement_overrides) as connection:
        connection.execute("SELECT value FROM json_each('[]')").fetchall()
        connection.execute("PRAGMA query_only = ON")
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 64_000)
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 100_000)
        connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 64)
        started = time.monotonic()
        steps = 0

        def progress() -> int:
            """Abort after roughly two million VM operations or the time limit."""
            nonlocal steps
            steps += 1000
            return int(steps > 2_000_000 or time.monotonic() - started > timeout_seconds)

        def authorize(action: int, arg1: str | None, arg2: str | None,
                      database: str | None, trigger: str | None) -> int:
            """Allow only reads, SELECTs, recursion, and a small set of SQL functions."""
            if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE}:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in allowed_functions:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        connection.set_progress_handler(progress, 1000)
        connection.set_authorizer(authorize)
        try:
            cursor = connection.execute(sql, dict(params or {}))
            if cursor.description is None:
                raise ValueError("Only queries returning rows are allowed.")
            rows = []
            result_bytes = 0
            truncated = False
            # Stream rows so a large text SELECT cannot allocate gigabytes before
            # the row cap is applied. A separate 8 MiB payload cap protects RAM.
            while len(rows) < max_rows:
                row = cursor.fetchone()
                if row is None:
                    break
                row_bytes = sum(len(value.encode("utf-8")) if isinstance(value, str)
                                else len(value) if isinstance(value, bytes) else 8 for value in row)
                if result_bytes + row_bytes > 8 * 1024 * 1024:
                    truncated = True
                    break
                rows.append(row)
                result_bytes += row_bytes
            if len(rows) == max_rows:
                truncated = cursor.fetchone() is not None
            result = pd.DataFrame(rows, columns=[column[0] for column in cursor.description])
            result.attrs["truncated"] = truncated
            return result
        except sqlite3.Error as exc:
            raise ValueError(f"SQL query rejected or failed: {exc}") from exc


def get_country_energy(db_path: str | Path, countries: Sequence[str] | None = None,
                       start_year: int = 2000, end_year: int = REFERENCE_YEAR) -> pd.DataFrame:
    """Read national energy observations in their original TWh/percent units."""
    return _read(db_path, ENERGY_SQL, query_parameters(countries, start_year=start_year, end_year=end_year))


def get_generation_annual(db_path: str | Path, countries: Sequence[str] | None = None,
                          start_year: int = 2008, end_year: int = REFERENCE_YEAR) -> pd.DataFrame:
    """Sum a fuel only when all twelve monthly values exist; incomplete years stay NULL."""
    return _read(db_path, GENERATION_SQL, query_parameters(countries, start_year=start_year, end_year=end_year))
