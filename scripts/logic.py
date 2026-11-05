"""Pure, auditable scenario calculations; no database or presentation dependencies.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from statistics import median, quantiles
from typing import Any

from scripts.constants import (
    COAL_CAPACITY_FACTOR,
    HIGH_URGENCY_YEAR,
    HOURS_PER_YEAR,
    MEDIUM_URGENCY_YEAR,
    REFERENCE_YEAR,
)

URGENCY_TIERS = (
    "Critical / Overdue Decommission",
    f"High Urgency (Phase-out by {HIGH_URGENCY_YEAR})",
    f"Medium Urgency ({HIGH_URGENCY_YEAR + 1}-{MEDIUM_URGENCY_YEAR} Target)",
    "Long-Term Transition / Non-Compliant",
)


def finite_number(value: Any, name: str, minimum: float = 0.0) -> float:
    """Return a finite float at least ``minimum`` or raise a descriptive ValueError."""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric.") from exc
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{name} must be finite and at least {minimum}.")
    return number


def _missing(value: Any) -> bool:
    """Recognize nullable scalar representations without requiring pandas."""
    return value is None or str(value).strip().lower() in {
        "", "nan", "none", "nat", "<na>", "unknown", "n/a"
    }


def _peer_groups(unit: dict) -> list[tuple[str, str]]:
    """Return unit peer keys in country, bloc, then Europe order."""
    return [
        (level, str(name))
        for level, name in [
            ("country", unit.get("country_name")),
            ("bloc", unit.get("region_bloc")),
            ("Europe", "Europe"),
        ]
        if not _missing(name)
    ]


def _observed_peer_pools(units: list[dict]) -> dict[tuple[str, str], list[int]]:
    """Collect only observed commissioning years from non-retired assets."""
    pools: dict[tuple[str, str], list[int]] = {}
    for donor in units:
        if donor.get("status") == "Retired" or _missing(donor.get("commission_year")):
            continue
        for key in _peer_groups(donor):
            pools.setdefault(key, []).append(int(donor["commission_year"]))
    return pools


def _select_peer_cohort(
        unit: dict, pools: dict[tuple[str, str], list[int]],
) -> tuple[str, list[int]] | None:
    """Choose the narrowest unit cohort with at least five observations."""
    for key in _peer_groups(unit):
        cohort = pools.get(key, [])
        if len(cohort) >= 5:
            return key[1], cohort
    return None


def classify_decommissioning_urgency(
        units_list: list[dict], lifespan_threshold: int | None = None,
        reference_year: int = REFERENCE_YEAR, estimate_missing: bool = True,
        estimate_from_peers: bool = False,
) -> list[dict]:
    """Copy unit records and assign estimated retirement years and urgency tiers.

    Reported dates are preserved. Missing dates use commissioning + the supplied
    lifespan, defaulting to 45 years. The same lifespan applies to peer estimates.
    Optional peer estimates use observed commissioning years from at least five
    unit records, preferring country, then bloc, then the European dataset.
    Peer quartiles describe cohort spread, not a prediction confidence interval.
    Missing commissioning observations are never filled. Cohorts must be built
    before UI filtering so selecting a country cannot change its estimates.
    Retired units are explicitly separated from the active urgency categories.
    """
    if lifespan_threshold is not None and not 35 <= lifespan_threshold <= 50:
        raise ValueError("Lifespan threshold must be between 35 and 50 years.")
    lifespan = lifespan_threshold if lifespan_threshold is not None else 45
    pools = _observed_peer_pools(units_list) if estimate_from_peers else {}
    classified = []
    for original in units_list:
        unit = dict(original)
        commission = unit.get("commission_year")
        age = None if _missing(commission) else reference_year - int(commission)
        unit["plant_age"] = age
        modeled = None
        unit.update(model_status="Missing commissioning year", retirement_peer_group=None,
                    retirement_peer_count=None, retirement_peer_p25=None, retirement_peer_p75=None)
        if unit.get("status") == "Retired":
            unit["model_status"] = "Retired"
        elif age is not None:
            modeled = int(commission) + lifespan
            unit["model_status"] = "Modeled · commissioning + lifespan"
        elif estimate_from_peers:
            peer = _select_peer_cohort(unit, pools)
            if peer is not None:
                name, cohort = peer
                modeled = math.floor(median(cohort) + lifespan + 0.5)
                quartiles = quantiles(cohort, n=4, method="inclusive")
                unit.update(model_status="Modeled · peer median", retirement_peer_group=str(name),
                            retirement_peer_count=len(cohort),
                            retirement_peer_p25=math.floor(quartiles[0] + lifespan),
                            retirement_peer_p75=math.ceil(quartiles[2] + lifespan))
        unit["modeled_retire_year"] = modeled
        retirement = unit.get("target_retire_year")
        estimated = bool(unit.get("is_retirement_estimated", False)) or _missing(retirement)
        unit["is_retirement_estimated"] = estimated
        if estimated:
            retirement = modeled if estimate_missing else None
        unit["target_retire_year"] = None if _missing(retirement) else int(retirement)
        if unit.get("status") == "Retired":
            unit["urgency_tier"] = "Retired"
            unit["years_remaining"] = None
        elif _missing(retirement):
            unit["urgency_tier"] = (
                "Unknown commissioning year" if estimate_missing and age is None
                else "Retirement date unavailable"
            )
            unit["years_remaining"] = None
        else:
            years_remaining = int(retirement) - reference_year
            unit["years_remaining"] = years_remaining
            if years_remaining <= 0:
                tier = URGENCY_TIERS[0]
            elif int(retirement) <= HIGH_URGENCY_YEAR:
                tier = URGENCY_TIERS[1]
            elif int(retirement) <= MEDIUM_URGENCY_YEAR:
                tier = URGENCY_TIERS[2]
            else:
                tier = URGENCY_TIERS[3]
            unit["urgency_tier"] = tier
        classified.append(unit)
    return classified


def calculate_just_transition_vulnerability(
        region_data: dict, weights: dict | None = None,
) -> float:
    """Return a bounded 0–100 illustrative vulnerability screening score.

    coal_share is a fraction [0,1]; jobs_per_1000 saturates at 50; GDP index is
    EU27=100 and deprivation saturates at GDP=0. Default weights are 0.4/0.35/0.25.
    A non-EU region receives a 10-point penalty for lacking direct JTF access.
    Missing inputs raise ValueError; no observations are filled or generated.
    """
    selected = weights if weights is not None else {
        "coal_share": 0.40, "jobs_per_1000": 0.35, "gdp_index": 0.25,
    }
    expected = {"coal_share", "jobs_per_1000", "gdp_index"}
    if set(selected) != expected:
        raise ValueError(f"Weights must contain exactly {sorted(expected)}.")
    clean_weights = {}
    for name, weight in selected.items():
        clean_weights[name] = finite_number(weight, f"weight {name}")
    if not math.isclose(sum(clean_weights.values()), 1.0, abs_tol=1e-8):
        raise ValueError("Weights must sum to one.")
    coal = finite_number(region_data.get("coal_share"), "coal_share")
    if coal > 1:
        raise ValueError("coal_share must be a fraction between zero and one.")
    jobs = finite_number(region_data.get("jobs_per_1000"), "jobs_per_1000")
    gdp = finite_number(region_data.get("gdp_index"), "gdp_index")
    membership = region_data.get("in_eu")
    if membership not in (True, False, 0, 1) or membership is None:
        raise ValueError("in_eu must be a boolean.")
    components = {
        "coal_share": coal,
        "jobs_per_1000": min(jobs / 50.0, 1.0),
        "gdp_index": max(0.0, 1.0 - gdp / 100.0),
    }
    score = 0.0
    for indicator, value in components.items():
        score += clean_weights[indicator] * value * 100
    if not membership:
        score += 10.0
    return round(min(100.0, max(0.0, score)), 2)


def simulate_clean_replacement_needs(
        capacity_mw: float, replacement_mix: dict,
        capex_eur_per_kw: Mapping[str, float] | None = None,
) -> dict:
    """Match coal annual energy with solar/wind nameplate capacity and simple CapEx.

    replacement_mix holds energy fractions, e.g. {"Solar PV": 0.5, "Wind": 0.5}.
    Solar CF=0.15, wind CF=0.30; coal CF=0.65. Costs default to 700/1400 EUR/kW.
    Results exclude storage, transmission, financing, and hourly firm capacity.
    Coal subtype is not required: the same coal capacity factor applies throughout.
    """
    capacity = finite_number(capacity_mw, "capacity_mw")
    factors = {"Solar PV": 0.15, "Wind": 0.30}
    costs = dict(capex_eur_per_kw) if capex_eur_per_kw is not None else {
        "Solar PV": 700.0, "Wind": 1400.0,
    }
    if not replacement_mix or not set(replacement_mix).issubset(factors):
        raise ValueError("Replacement mix may contain Solar PV and Wind only.")
    shares = {name: finite_number(share, name) for name, share in replacement_mix.items()}
    if not math.isclose(sum(shares.values()), 1.0, abs_tol=1e-8):
        raise ValueError("Replacement energy fractions must sum to one.")
    annual_gwh = capacity * HOURS_PER_YEAR * COAL_CAPACITY_FACTOR / 1000.0
    technologies = {}
    total_capex = 0.0
    for technology, share in shares.items():
        cost = finite_number(costs.get(technology), f"{technology} CapEx")
        energy = annual_gwh * share
        required_mw = energy * 1000.0 / (HOURS_PER_YEAR * factors[technology])
        capex = required_mw * 1000.0 * cost
        technologies[technology] = {
            "annual_gwh": energy, "required_capacity_mw": required_mw,
            "capex_eur": capex, "capacity_factor": factors[technology],
        }
        total_capex += capex
    return {"annual_generation_gwh": annual_gwh, "technologies": technologies,
            "total_capex_eur": total_capex}


def calculate_transition_pressure_components(region_data: dict) -> dict[str, float] | None:
    """Return weighted score contributions before the 100-point cap, or None for gaps."""
    names = ["national_coal_share_pct", "employment_rate_20_64", "gdp_index"]
    for name in names:
        if _missing(region_data.get(name)):
            return None
    coal = finite_number(region_data["national_coal_share_pct"], "National coal share")
    employment = finite_number(region_data["employment_rate_20_64"], "Employment rate")
    gdp = finite_number(region_data["gdp_index"], "GDP index")
    if coal > 100 or employment > 100:
        raise ValueError("Coal shares and employment rates must be between 0 and 100.")
    return {
        "coal_dependence": 40 * coal / 100,
        "employment_shortfall": 35 * (1 - employment / 100),
        "gdp_deprivation": 25 * max(0, 1 - gdp / 100),
        "non_eu_adjustment": 0.0 if region_data.get("in_eu") else 10.0,
    }


def calculate_observed_transition_pressure(region_data: dict) -> float | None:
    """Score observed coal dependence and regional resilience, or return None for gaps.

    Uses national coal electricity share as an explicitly national proxy, regional
    employment rate aged 20–64, and source EUR/person GDP EU27 index. Weights are
    0.40/0.35/0.25; non-EU membership adds 10 points. Employment shortfall is not an
    unemployment rate or mining-job estimate. The result is an analytical score,
    not an observation, legal eligibility decision or prediction of redundancies.
    """
    components = calculate_transition_pressure_components(region_data)
    if components is None:
        return None
    return round(min(100.0, max(0.0, sum(components.values()))), 2)
