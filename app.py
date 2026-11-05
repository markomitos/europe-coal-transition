"""Streamlit executive dashboard; run with ``streamlit run app.py``."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Callable

import matplotlib.pyplot as plt
import pandas as pd
import pydeck as pdk
import streamlit as st
from matplotlib.colors import to_rgb
from matplotlib.figure import Figure

from scripts import queries as q
from scripts import visualizations as viz
from scripts.constants import (
    COAL_CAPACITY_FACTOR,
    HIGH_URGENCY_YEAR,
    HOURS_PER_YEAR,
    OBSERVATION_YEAR,
    REFERENCE_YEAR,
)
from scripts.logic import (
    calculate_observed_transition_pressure,
    classify_decommissioning_urgency,
    simulate_clean_replacement_needs,
)
from scripts.setup_db import DEFAULT_DB_PATH, read_metadata, setup_database
from scripts.gem_data import GEM_SNAPSHOT

st.set_page_config(page_title="Coal Transition | Europe & Balkans", page_icon="🌍", layout="wide")


@st.cache_resource
def plotting_lock() -> threading.RLock:
    """Serialize Matplotlib rendering across concurrent Streamlit sessions."""
    return threading.RLock()


@st.cache_data(ttl=3600, max_entries=8)
def load_units(path: str, revision: tuple[int, int]) -> pd.DataFrame:
    """Cache a compact joined unit table; invalidate when the file changes."""
    return q.get_units(path)


@st.cache_data(ttl=3600, max_entries=24)
def load_classified_units(
        path: str, revision: tuple[int, int], lifespan: int,
        model_dates: bool = True, peer_dates: bool = True,
) -> pd.DataFrame:
    """Cache full-fleet estimates independently of country and status filters."""
    original = load_units(path, revision)
    records = original.astype(object).where(original.notna(), None).to_dict("records")
    classified = pd.DataFrame(classify_decommissioning_urgency(
        records, lifespan, estimate_missing=model_dates, estimate_from_peers=peer_dates,
    ))
    for column in ["modeled_retire_year", "retirement_peer_count", "retirement_peer_p25", "retirement_peer_p75"]:
        classified[column] = pd.to_numeric(classified[column], errors="coerce").astype("Int64")
    return classified


@st.cache_data(ttl=3600, max_entries=24)
def load_scenario(
        path: str, revision: tuple[int, int], countries: tuple[str, ...],
        statuses: tuple[str, ...], lifespan: int, model_dates: bool = True, peer_dates: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, int | None]]:
    """Filter cached estimates and calculate regional summaries and country retirement curves."""
    classified = load_classified_units(path, revision, lifespan, model_dates, peer_dates)
    overrides = {
        unit_id: None if pd.isna(year) else int(year)
        for unit_id, year in zip(classified["unit_id"], classified["target_retire_year"])
    }
    units = classified[classified["country_name"].isin(countries) & classified["status"].isin(statuses)].copy()
    regions = q.get_regional_capacity_summary(path, countries, statuses,
                                              retirement_overrides=overrides, minimum_capacity=0)
    if not regions.empty:
        energy = q.get_country_energy(path, start_year=OBSERVATION_YEAR, end_year=OBSERVATION_YEAR)
        coal_share = energy.set_index("country_iso")["coal_share_elec"].to_dict()
        countries_by_name = classified.set_index("country_name")["country_iso"].to_dict()
        regions["national_coal_share_pct"] = regions["country_name"].map(countries_by_name).map(coal_share)
        regions["vulnerability_index"] = [
            calculate_observed_transition_pressure(row)
            for row in regions.to_dict("records")
        ]
        units = units.merge(regions[["region_id", "vulnerability_index"]], on="region_id", how="left")
    else:
        regions["vulnerability_index"] = pd.Series(dtype=float)
        units["vulnerability_index"] = pd.Series(dtype=float)
    timeline = q.get_phaseout_timeline(path, 2020, 2040, countries, statuses, overrides)
    return units, regions, timeline, overrides


def show_figure(function: Callable[[pd.DataFrame], Figure], frame: pd.DataFrame) -> None:
    """Render and immediately close a figure to bound per-session RAM use."""
    with plotting_lock():
        figure = function(frame)
        try:
            st.pyplot(figure, width="stretch", alt=function.__doc__)
        finally:
            plt.close(figure)


def render_sql_console(path: str, countries: list[str], statuses: list[str], overrides: dict) -> None:
    """Run predefined analytical queries with the current scenario and filters."""
    st.subheader("SQLite analytical queries")
    template = st.selectbox("Analytical query", list(q.QUERY_CATALOG), key="query_template")
    params = q.query_parameters(countries, statuses, start_year=2020)
    if st.button("Run query", type="primary", key="run_sql"):
        try:
            result = q.run_readonly_query(path, q.QUERY_CATALOG[template], params, overrides)
            st.dataframe(result, hide_index=True, width="stretch")
            if result.attrs.get("truncated"):
                st.info("Result truncated at the 2,000-row or 8 MiB limit.")
            else:
                st.caption(f"{len(result):,} rows returned. Source database unchanged.")
        except ValueError as exc:
            st.error(str(exc))


def render_simulator(path: str, units: pd.DataFrame) -> None:
    """Simulate solar/wind annual-energy replacement for selected operating units."""
    active = units[~units["status"].isin(["Retired", "Mothballed"])].copy()
    if active.empty:
        st.info("Select a plant not reported as retired or mothballed to simulate clean replacement.")
        return
    plants = active[["plant_id", "plant_name", "country_name"]].drop_duplicates().sort_values(
        ["country_name", "plant_name"])
    labels = {row["plant_id"]: f"{row['plant_name']} · {row['country_name']}" for row in plants.to_dict("records")}
    plant = st.selectbox("Target project", plants["plant_id"].tolist(), format_func=labels.get)
    selected = active[active["plant_id"] == plant]
    plant_capacity = float(selected["capacity_mw"].sum())
    target_capacity = st.number_input("Thermal capacity to replace (MW)", min_value=0.0,
                                      max_value=plant_capacity, value=plant_capacity, step=10.0,
                                      key=f"capacity_{plant}")
    solar_column, wind_column = st.columns(2)
    with solar_column:
        solar_cost = st.slider("Solar CapEx (€/kW)", 300, 2000, 700, 50,
                               key="solar_cost", persist_state="session")
    with wind_column:
        wind_cost = st.slider("Wind CapEx (€/kW)", 600, 3000, 1400, 50,
                              key="wind_cost", persist_state="session")
    solar_share = st.slider("Solar share of replacement annual energy (%)", 0, 100, 50, 5)
    result = simulate_clean_replacement_needs(target_capacity,
                                              {"Solar PV": solar_share / 100, "Wind": 1 - solar_share / 100},
                                              {"Solar PV": solar_cost, "Wind": wind_cost})
    generation_card, solar_card, wind_card = st.columns(3)
    generation_card.metric("Annual generation to replace", f"{result['annual_generation_gwh'] / 1000:,.2f} TWh")
    solar_card.metric("Solar PV nameplate", f"{result['technologies']['Solar PV']['required_capacity_mw']:,.0f} MW")
    wind_card.metric("Wind nameplate", f"{result['technologies']['Wind']['required_capacity_mw']:,.0f} MW")
    st.metric("Estimated generation CapEx", f"€{result['total_capex_eur'] / 1e9:,.2f} bn")
    breakdown = pd.DataFrame(result["technologies"]).T.rename_axis("Technology").reset_index()
    st.dataframe(breakdown, hide_index=True, width="stretch")
    country_name = str(selected["country_name"].iloc[0])
    actual_generation = q.get_generation_annual(path, [country_name], OBSERVATION_YEAR, OBSERVATION_YEAR)
    st.subheader(f"Observed national generation · {country_name} · {OBSERVATION_YEAR}")
    if not actual_generation.empty:
        show_figure(viz.plot_generation_pie, actual_generation)
        st.caption("Other / unclassified covers generation outside the available fuel breakdown.")
        with st.expander("Generation data (GWh)"):
            st.dataframe(actual_generation, hide_index=True, width="stretch",
                         alt="Reported complete-year national electricity generation in GWh")
    else:
        st.info("No complete-year national generation record for this country.")
    st.caption(
        "Existing renewable output is context, not spare energy automatically available to replace the selected plant.")
    st.caption(
        "Energy-equivalent screening estimate: coal CF 65%, solar 15%, wind 30%. Excludes storage, grids, financing and hourly reliability; matching annual energy does not ensure firm capacity.")


def render_map(units: pd.DataFrame) -> None:
    """Show all countries with the selected retired-unit visibility and lifespan scenario."""
    st.subheader("Coal unit locations · source snapshot")
    st.caption("The map shows all countries. Show retired plants controls retired-unit visibility; the assumed lifespan determines urgency colors.")
    points = units.dropna(subset=["latitude", "longitude"]).copy()
    # Keep each reported project location, colored by its most urgent unit.
    priority = {tier: rank for rank, tier in enumerate(viz.URGENCY_COLORS)}
    points["priority"] = points["urgency_tier"].map(priority)
    points = points.sort_values("priority").drop_duplicates(["plant_id", "latitude", "longitude"])
    if not points.empty:
        points["color"] = [
            [round(channel * 255) for channel in to_rgb(viz.URGENCY_COLORS[tier])]
            for tier in points["urgency_tier"]
        ]
        plant_map = pdk.Deck(
            map_style=None,
            initial_view_state=pdk.ViewState(
                latitude=float(points["latitude"].mean()),
                longitude=float(points["longitude"].mean()),
                zoom=4,
            ),
            layers=[pdk.Layer(
                "ScatterplotLayer",
                data=points[["latitude", "longitude", "color", "plant_name", "country_name", "urgency_tier"]],
                get_position="[longitude, latitude]",
                get_fill_color="color",
                get_radius=80,
                radius_min_pixels=3,
                pickable=True,
                auto_highlight=True,
            )],
            tooltip={"text": "{plant_name}\n{country_name}\n{urgency_tier}"},
        )
        st.pydeck_chart(
            plant_map, width="stretch",
            alt="GEM plant locations colored by retirement scenario; hover over a marker for its plant name, country and retirement urgency",
        )
        show_figure(viz.plot_map_legend, points)
    else:
        st.info("No valid coordinates in this selection. Units remain in the analysis.")


def render_metrics(units: pd.DataFrame, model_dates: bool) -> None:
    """Summarize the filtered units and their selected retirement scenario."""
    active = units[units["status"] != "Retired"]
    due = active[(active["status"] != "Mothballed") & (
                pd.to_numeric(active["target_retire_year"], errors="coerce") <= HIGH_URGENCY_YEAR)]
    generation_gap = float(due["capacity_mw"].sum()) * HOURS_PER_YEAR * COAL_CAPACITY_FACTOR / 1_000_000
    cards = st.columns(3)
    cards[0].metric("Total monitored capacity", f"{units['capacity_mw'].sum():,.0f} MW")
    cards[1].metric("Non-retired units older than 40 years", f"{int((active['plant_age'] > 40).sum()):,}")
    cards[2].metric(f"Projected {HIGH_URGENCY_YEAR} generation gap",
                    f"{generation_gap:,.2f} TWh" if model_dates else "Not modeled")
    missing_retirement = int(units["is_retirement_estimated"].sum())
    st.caption(
        f"{len(units):,} GEM units · {units['plant_id'].nunique():,} projects · snapshot {GEM_SNAPSHOT} · {missing_retirement:,} missing future retirement commitments")


def render_asset_records(units: pd.DataFrame, lifespan: int) -> None:
    """Display filtered unit observations alongside explicit model outputs."""
    with st.expander("Joined asset records"):
        asset_table = units.drop(columns=["target_retire_year"]).rename(columns={
            "modeled_retire_year": "Modeled retirement dates",
            "model_status": "Model status",
            "source_snapshot": "Source snapshot",
            "source_url": "GEM source",
            "retirement_peer_group": "Peer group",
            "retirement_peer_count": "Observed peers",
            "retirement_peer_p25": "Peer retirement P25",
            "retirement_peer_p75": "Peer retirement P75",
        })
        leading_columns = ["plant_name", "unit_name", "country_name", "status", "Source snapshot", "GEM source",
                           "owner_name", "capacity_mw", "commission_year", "closure_year",
                           "Modeled retirement dates", "Model status", "Peer group", "Observed peers",
                           "Peer retirement P25", "Peer retirement P75"]
        asset_table = asset_table[
            leading_columns + [column for column in asset_table if column not in leading_columns]]
        st.dataframe(asset_table.head(2000), hide_index=True, width="stretch",
                     alt="Asset records with modeled retirement years and model details")
        st.caption(
            f"Model: commissioning year + {lifespan} years; missing years optionally use peer medians. P25–P75 is peer spread, not a confidence interval. Source years stay blank.")


def main() -> None:
    """Render the dashboard with reproducible data provenance and coherent controls."""
    st.title("Coal phase-out & just transition")
    st.write("Track aging coal assets, test retirement pathways, and explore the regional cost of transition.")
    try:
        with st.spinner("Preparing the local SQLite dataset…"):
            path = setup_database(DEFAULT_DB_PATH)
            stat = path.stat()
            revision = (stat.st_mtime_ns, stat.st_size)
            metadata = read_metadata(path)
            all_units = load_units(str(path), revision)
    except (ValueError, OSError, sqlite3.Error) as exc:
        st.error(f"Dataset setup failed: {exc}")
        st.code("python scripts/setup_db.py --force", language="bash")
        st.stop()
    with st.sidebar:
        st.header("Explore the transition")
        available = sorted(all_units["country_name"].unique())
        default_countries = [country for country in ["Poland", "Serbia", "Germany"] if country in available]
        countries = st.multiselect(
            "Target countries", available, default=default_countries, key="countries",
            help="Filters every graph, analysis table and replacement project. The Overview map always shows all countries.",
        )
        show_retired = st.toggle(
            "Show retired plants", value=True, key="show_retired",
            help="Include retired GEM units on the map and in analysis graphs and tables. Operating and mothballed units remain included.",
        )
        statuses = [status for status in sorted(all_units["status"].unique())
                    if show_retired or status != "Retired"]
        model_dates = st.toggle("Enable modeled retirement dates", value=True, key="model_dates",
                                help="Lifespan assumptions create a scenario, not reported retirement commitments.")
        peer_dates = st.toggle("Estimate dates with missing commissioning years", value=True, key="peer_dates",
                               help="Uses at least five observed peers: same country, then EU or Non-EU group, then Europe. Source years stay missing.",
                               disabled=not model_dates)
        lifespan = st.slider("Assumed asset lifespan (years)", 35, 50, 45, key="lifespan",
                             help="Updates map urgency colors and retirement scenarios when modeled dates are enabled. Reported dates remain fixed.",
                             disabled=not model_dates)
        st.caption(f"Reference year: {REFERENCE_YEAR}")

    overview, analysis, transition, replacement = st.tabs([
        "01  Overview", "02  Analysis & SQL", "03  Just transition", "04  Clean replacement"],
        key="dashboard_view", on_change="rerun")
    if overview.open:
        with overview:
            map_units = load_classified_units(str(path), revision, lifespan, model_dates, peer_dates)
            if not show_retired:
                map_units = map_units[map_units["status"].ne("Retired")]
            render_map(map_units)
    else:
        units, regions, timeline, overrides = load_scenario(
            str(path), revision, tuple(countries), tuple(statuses), lifespan, model_dates, peer_dates,
        )
        if units.empty:
            st.info("No assets match the filters. Select a country or enable retired plants.")
        else:
            with analysis:
                if analysis.open:
                    render_metrics(units, model_dates)
                    show_figure(viz.plot_age_distribution, units)
                    st.caption("Each color represents a country. Ages use observed commissioning years; retired units are excluded.")
                    show_figure(viz.plot_country_status, units)
                    show_figure(viz.plot_generation_mix,
                                q.get_generation_annual(str(path), countries, OBSERVATION_YEAR, OBSERVATION_YEAR))
                    if model_dates or units["target_retire_year"].notna().any():
                        show_figure(viz.plot_phaseout_timeline, timeline)
                    else:
                        st.info("No reported retirement commitments in this source. Enable the modeled retirement scenario to explore a lifespan assumption.")
                    with st.expander("Annual trajectory, including estimated MW"):
                        st.dataframe(timeline, hide_index=True, width="stretch")
                    show_figure(viz.plot_asset_capacity, units)
                    show_figure(viz.plot_regional_exposure, regions)
                    show_figure(viz.plot_correlation_heatmap, units)
                    st.caption("Regional charts use observed employment and GDP with the calculated screening score. Missing inputs remain unavailable; correlations do not establish causality.")
                    render_asset_records(units, lifespan)
                    st.subheader("Reported ownership groups from the source snapshot")
                    owner_data = q.get_owner_capacity_share(str(path), countries, statuses, top_n=100)
                    st.dataframe(owner_data, hide_index=True, width="stretch",
                                 alt="Reported ownership-group capacities, national shares and ranks")
                    st.caption("Ownership groups retain GEM’s reported text and stakes. Capacities are grouped by that full text; company stakes are not allocated and owners are not assumed to be operators.")
                    render_sql_console(str(path), countries, statuses, overrides)

            with transition:
                if transition.open:
                    st.subheader("Where transition support matters most")
                    st.write("The vulnerability index is a custom 0–100 screening score. Higher values indicate greater potential difficulty in moving away from coal.")
                    st.caption(
                        "Screening score = 40% national coal-electricity dependence + 35% regional employment shortfall + 25% GDP deprivation, plus 10 points outside the EU; bounded to 0–100. National coal share is a proxy, not measured regional dependence. The employment shortfall is not an unemployment rate. The GDP index is EUR/person relative to EU27, not PPS. Missing inputs produce no score.")
                    st.caption("Colored segments show each component’s contribution in points; the label at the end is the total score. The dashed line marks the 100-point score cap.")
                    st.caption(
                        f"Eurostat observations via Kaggle, matched to GISCO NUTS-2 and UK ONS ITL2 regions. Regional year: {metadata['regional_year']}. UK GDP and employment are unavailable in the current sources. Blank values stay unavailable. General employment rates do not measure coal-mining jobs.")
                    ranking = regions.sort_values("vulnerability_index", ascending=False, na_position="last").copy()
                    show_figure(viz.plot_highest_vulnerability, ranking)
                    show_figure(viz.plot_lowest_vulnerability, ranking)
                    st.dataframe(ranking[["country_name", "region_name", "employment_rate_20_64",
                                          "gdp_per_capita_eur", "gdp_index", "national_coal_share_pct", "vulnerability_index",
                                          "total_capacity_mw"]],
                                 hide_index=True, width="stretch",
                                 alt="Observed regional employment and GDP with calculated screening scores")

            with replacement:
                if replacement.open:
                    st.subheader("Plan annual-energy replacement")
                    render_simulator(str(path), units)

    st.divider()
    st.caption(
        "Sources: official GEM GCPT unit data, GISCO and UK ONS ITL2 boundaries, and four linked Kaggle datasets. Replacement costs and retirement scenarios are modeled. See README.md for methodology.")


if __name__ == "__main__":
    main()
