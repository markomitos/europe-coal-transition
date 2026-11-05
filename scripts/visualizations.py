"""Standalone Seaborn/Matplotlib figures with safe empty-data behavior."""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from scripts.constants import HIGH_URGENCY_YEAR, MEDIUM_URGENCY_YEAR, OBSERVATION_YEAR, REFERENCE_YEAR
from scripts.logic import URGENCY_TIERS, calculate_transition_pressure_components

URGENCY_COLORS = dict(zip(URGENCY_TIERS, ["#b83b40", "#df8e32", "#347d90", "#4e62aa"]))
URGENCY_COLORS.update(
    {"Retired": "#87949c", "Retirement date unavailable": "#6c737a", "Unknown commissioning year": "#6c737a"})


def plot_map_legend(points: pd.DataFrame) -> Figure:
    """Return a compact chart legend using the exact colors of visible map markers."""
    labels = {
        URGENCY_TIERS[0]: "Critical / overdue",
        URGENCY_TIERS[1]: f"High urgency · by {HIGH_URGENCY_YEAR}",
        URGENCY_TIERS[2]: f"Medium urgency · {HIGH_URGENCY_YEAR + 1}–{MEDIUM_URGENCY_YEAR}",
        URGENCY_TIERS[3]: f"Long-term transition · after {MEDIUM_URGENCY_YEAR}",
        "Retired": "Retired",
        "Retirement date unavailable": "Retirement date unavailable",
        "Unknown commissioning year": "Commissioning year unavailable",
    }
    present = set(points["urgency_tier"].dropna()) if not points.empty else set()
    handles = [
        Line2D([], [], linestyle="none", marker="o", markersize=9,
               markerfacecolor=color, markeredgecolor="none", label=labels[tier])
        for tier, color in URGENCY_COLORS.items() if tier in present
    ]
    rows = max(1, (len(handles) + 2) // 3)
    figure = Figure(figsize=(10, 0.45 + rows * 0.4), layout="constrained")
    axes = figure.subplots()
    axes.set_axis_off()
    if handles:
        axes.legend(handles=handles, loc="center", ncol=min(3, len(handles)),
                    frameon=False, fontsize=10, handletextpad=0.4, columnspacing=1.8)
    return figure


def _canvas(title: str) -> tuple[Figure, plt.Axes]:
    """Create a consistent figure without changing global plotting style."""
    with plt.rc_context({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False}):
        figure, axes = plt.subplots(figsize=(9, 4.8), layout="constrained")
    figure.set_facecolor("#ffffff")
    axes.set_title(title, loc="left", fontsize=13, fontweight="bold", pad=15)
    axes.set_axisbelow(True)
    axes.grid(axis="y", alpha=0.16)
    return figure, axes


def _empty(axes: plt.Axes, message: str = "No matching observations for this selection") -> None:
    """Label an empty figure instead of failing or inventing observations."""
    axes.text(0.5, 0.5, message, ha="center", va="center", transform=axes.transAxes, color="#65727d")
    axes.set_xticks([])
    axes.set_yticks([])


def plot_age_distribution(units: pd.DataFrame) -> Figure:
    """Compare non-retired unit ages by country, with optional density curves."""
    figure, axes = _canvas(f"Age of coal units · reference year {REFERENCE_YEAR}")
    if units.empty:
        _empty(axes)
        return figure
    data = units[units["status"] != "Retired"].dropna(subset=["plant_age"]).copy()
    if data.empty:
        _empty(axes, "No units with known commissioning years")
        return figure
    data["plant_age"] = data["plant_age"].astype(float)
    countries = sorted(data["country_name"].unique())
    palette = dict(zip(countries, sns.color_palette("husl", len(countries))))
    use_kde = all(group["plant_age"].nunique() > 2 for _, group in data.groupby("country_name"))
    sns.histplot(data=data, x="plant_age", hue="country_name", hue_order=countries, multiple="layer",
                 bins=12, kde=use_kde, palette=palette, alpha=0.4, ax=axes)
    axes.get_legend().set_title("Country")
    for year, color in [(30, "#5a737a"), (40, "#b83b40")]:
        axes.axvline(year, color=color, linestyle="--", linewidth=1.2)
        axes.text(year + 0.4, 0.95, f"{year} years", transform=axes.get_xaxis_transform(), va="top", color=color)
    axes.set(xlabel="Unit age (years)", ylabel="Units")
    return figure


def plot_country_status(units: pd.DataFrame) -> Figure:
    """Historical source capacity grouped by reported GEM unit status."""
    figure, axes = _canvas("GEM unit capacity by country and reported status")
    if units.empty:
        _empty(axes)
        return figure
    statuses = ["Operating", "Retired", "Mothballed"]
    totals = units.pivot_table(index="country_name", columns="status", values="capacity_mw",
                               aggfunc="sum", fill_value=0).reindex(columns=statuses, fill_value=0)
    totals = totals.loc[totals.sum(axis=1).sort_values().index]
    figure.set_size_inches(9, max(4.8, len(totals) * 0.38))
    left = np.zeros(len(totals))
    for status, color in zip(statuses, ["#347d90", "#b7c2c8", "#a58b65"]):
        axes.barh(totals.index, totals[status], left=left, label=status, color=color, height=0.65)
        left += totals[status].to_numpy()
    axes.legend(loc="lower right", fontsize=8)
    axes.set(xlabel="Nameplate capacity (MW)", ylabel="")
    return figure


def plot_asset_capacity(units: pd.DataFrame) -> Figure:
    """Scatter observed commissioning year vs capacity, colored by scenario urgency."""
    figure, axes = _canvas("Asset vintage, source capacity, and retirement scenario")
    if units.empty:
        _empty(axes)
        return figure
    data = units.dropna(subset=["commission_year", "capacity_mw"]).copy()
    if data.empty:
        _empty(axes, "No units with known commissioning years")
        return figure
    data["commission_year"] = data["commission_year"].astype(float)
    if "urgency_tier" not in data:
        data["urgency_tier"] = "Unknown commissioning year"
    sns.scatterplot(data=data, x="commission_year", y="capacity_mw", hue="urgency_tier",
                    palette=URGENCY_COLORS, s=70, alpha=0.75, ax=axes)
    axes.set(xlabel="Commissioning year", ylabel="Reported unit capacity (MW)")
    axes.legend(bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=7,
                title="Scenario tier")
    return figure


def plot_phaseout_timeline(timeline: pd.DataFrame) -> Figure:
    """Country step curves of non-retired capacity due for retirement, including estimated dates."""
    figure, axes = _canvas("Cumulative retirement scenario · selected countries")
    if timeline.empty:
        _empty(axes)
        return figure
    countries = sorted(timeline["country_name"].unique())
    palette = dict(zip(countries, sns.color_palette("husl", len(countries))))
    sns.lineplot(data=timeline, x="year", y="cumulative_retiring_mw", hue="country_name",
                 hue_order=countries, drawstyle="steps-post", estimator=None, linewidth=2.3,
                 palette=palette, ax=axes)
    axes.set(xlabel="Scenario retirement year", ylabel="Cumulative capacity due (MW)")
    axes.legend(title="Country", bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=8)
    axes.xaxis.set_major_locator(plt.MaxNLocator(integer=True, nbins=8))
    return figure


def plot_regional_exposure(regions: pd.DataFrame) -> Figure:
    """Observed regional employment vs GDP, colored by calculated screening score."""
    figure, axes = _canvas("Observed regional employment and GDP")
    if regions.empty:
        _empty(axes)
        return figure
    data = regions.dropna(subset=["employment_rate_20_64", "gdp_per_capita_eur", "vulnerability_index"])
    if data.empty:
        _empty(axes)
        return figure
    sns.scatterplot(data=data, x="employment_rate_20_64", y="gdp_per_capita_eur", hue="vulnerability_index",
                    palette="YlOrRd", hue_norm=(0, 100), s=100, edgecolor="white", ax=axes)
    if len(data) >= 3 and data["employment_rate_20_64"].nunique() > 1:
        sns.regplot(data=data, x="employment_rate_20_64", y="gdp_per_capita_eur", scatter=False,
                    ci=None, color="#8d969c", line_kws={"linestyle": "--", "linewidth": 1}, ax=axes)
    for _, row in data.iterrows():
        if row["region_name"] in {"Kolubara", "Kostolac", "Silesia", "Jiu Valley"} or row["vulnerability_index"] >= 70:
            axes.annotate(row["region_name"], (row["employment_rate_20_64"], row["gdp_per_capita_eur"]),
                          xytext=(5, 6), textcoords="offset points", fontsize=8)
    axes.set(xlabel="Employment rate aged 20–64 (%)", ylabel="Regional GDP per person (EUR)")
    axes.legend(title="Vulnerability / 100", fontsize=8)
    return figure


def _plot_vulnerability_ranking(regions: pd.DataFrame, *, highest: bool) -> Figure:
    """Rank up to ten regions and show the weighted components of each score."""
    direction = "highest" if highest else "lowest"
    figure, axes = _canvas(f"10 {direction} regional vulnerability scores")
    if regions.empty:
        _empty(axes, "No regions with a calculated vulnerability score")
        return figure
    required = ["vulnerability_index", "national_coal_share_pct", "employment_rate_20_64", "gdp_index"]
    if not set(required).issubset(regions.columns):
        _empty(axes, "No regions with complete score components")
        return figure
    data = regions.dropna(subset=required).sort_values(
        ["vulnerability_index", "country_name", "region_name"],
        ascending=[not highest, True, True],
    ).head(10)
    if data.empty:
        _empty(axes, "No regions with a calculated vulnerability score")
        return figure
    labels = data["region_name"] + " · " + data["country_name"]
    components = pd.DataFrame([
        calculate_transition_pressure_components(row) for row in data.to_dict("records")
    ])
    styles = {
        "coal_dependence": ("Coal dependence (40%)", "#347d90"),
        "employment_shortfall": ("Employment shortfall (35%)", "#a85e20"),
        "gdp_deprivation": ("GDP deprivation (25%)", "#3c7f5a"),
        "non_eu_adjustment": ("Non-EU adjustment (+10)", "#7b5797"),
    }
    figure.set_size_inches(11, max(4, len(data) * 0.42 + 2.2))
    totals = np.zeros(len(data))
    for column, (label, color) in styles.items():
        values = components[column].to_numpy()
        bars = axes.barh(range(len(data)), values, left=totals, color=color, label=label)
        axes.bar_label(bars, labels=[f"{value:.1f}" if value >= 4 else "" for value in values],
                       label_type="center", color="white", fontsize=8)
        totals += values
    for index, (total, score) in enumerate(zip(totals, data["vulnerability_index"])):
        suffix = " (capped)" if total > 100 + 1e-9 else ""
        axes.text(total + 1, index, f"{score:.1f}{suffix}", va="center", fontsize=9)
    axes.axvline(100, color="#555555", linestyle="--", linewidth=1, label="Score cap (100)")
    axes.set_yticks(range(len(data)), labels=labels, fontsize=9)
    axes.invert_yaxis()
    axes.set(xlabel="Weighted score contributions (points)", ylabel="",
             xlim=(0, 110))
    axes.legend(loc="upper left", bbox_to_anchor=(0, -0.14), ncol=2, frameon=False, fontsize=9)
    axes.grid(axis="y", visible=False)
    axes.grid(axis="x", alpha=0.16)
    return figure


def plot_highest_vulnerability(regions: pd.DataFrame) -> Figure:
    """Show the ten highest scores as stacked, weighted component contributions."""
    return _plot_vulnerability_ranking(regions, highest=True)


def plot_lowest_vulnerability(regions: pd.DataFrame) -> Figure:
    """Show the ten lowest scores as stacked, weighted component contributions."""
    return _plot_vulnerability_ranking(regions, highest=False)


def plot_correlation_heatmap(units: pd.DataFrame) -> Figure:
    """Pairwise Pearson correlations; constant or insufficient metrics remain masked."""
    figure, axes = _canvas("Unit-level metric correlations · descriptive only")
    columns = {"capacity_mw": "Capacity (MW)", "plant_age": "Age (years)",
               "gdp_per_capita_eur": "Regional GDP (EUR/person)", "employment_rate_20_64": "Employment (%)",
               "vulnerability_index": "Vulnerability"}
    if units.empty or not set(columns).issubset(units.columns):
        _empty(axes)
        return figure
    numeric = units[list(columns)].apply(pd.to_numeric, errors="coerce")
    correlation = numeric.rename(columns=columns).corr(min_periods=3)
    if correlation.isna().all().all():
        _empty(axes, "At least three observations and varying metrics are needed")
        return figure
    sns.heatmap(correlation, vmin=-1, vmax=1, center=0, cmap="vlag", annot=True,
                fmt=".2f", linewidths=0.6, square=True, cbar_kws={"label": "Pearson r"}, ax=axes)
    axes.tick_params(axis="x", labelrotation=30)
    return figure


def plot_generation_mix(generation: pd.DataFrame) -> Figure:
    """Plot reported complete-year coal, wind and solar generation, without filling gaps."""
    figure, axes = _canvas(f"Observed coal, wind and solar generation · {OBSERVATION_YEAR}")
    fields = ["coal_gwh", "wind_gwh", "solar_gwh"]
    if generation.empty:
        _empty(axes)
        return figure
    data = generation.melt(id_vars=["country_name"], value_vars=fields,
                           var_name="fuel", value_name="generation_gwh").dropna(subset=["generation_gwh"])
    if data.empty:
        _empty(axes, "No fuel has twelve reported monthly observations")
        return figure
    data["fuel"] = data["fuel"].map({"coal_gwh": "Coal / manufactured gases", "wind_gwh": "Wind", "solar_gwh": "Solar"})
    sns.barplot(data=data, x="country_name", y="generation_gwh", hue="fuel", errorbar=None, ax=axes)
    axes.set(xlabel="", ylabel="Annual reported generation (GWh)")
    axes.tick_params(axis="x", labelrotation=20)
    axes.legend(title="Source fuel grouping", fontsize=8)
    return figure


def plot_generation_pie(generation: pd.DataFrame) -> Figure:
    """Show one country's observed annual generation shares, without overlapping renewable totals."""
    figure, axes = _canvas("Observed national generation by source")
    if generation.empty:
        _empty(axes, "No complete-year national generation record")
        return figure
    if len(generation) != 1:
        raise ValueError("The generation pie requires exactly one country and year.")
    row = generation.iloc[0]
    sources = {
        "coal_gwh": "Coal / manufactured gases",
        "natural_gas_gwh": "Natural gas",
        "oil_gwh": "Oil",
        "nuclear_gwh": "Nuclear",
        "hydro_gwh": "Hydro",
        "wind_gwh": "Wind",
        "solar_gwh": "Solar",
    }
    values = pd.to_numeric(row.reindex(sources), errors="coerce").dropna()
    total = row.get("total_gwh")
    if (values.empty or not np.isfinite(values).all() or values.lt(0).any()
            or pd.isna(total) or not np.isfinite(total) or total <= 0):
        _empty(axes, "A positive annual total and source breakdown are required")
        return figure
    # Allow accumulated rounding of 12 monthly totals and seven fuels to 0.001 GWh.
    tolerance = max(0.05, total * 1e-9)
    remainder = total - values.sum()
    if remainder < -tolerance:
        _empty(axes, "Reported source totals exceed national generation")
        return figure
    values = values[values > 0].rename(index=sources)
    if remainder > tolerance:
        values.loc["Other / unclassified"] = remainder
    if values.empty:
        _empty(axes, "No positive source generation available")
        return figure
    wedges, _, _ = axes.pie(
        values, colors=sns.color_palette("Set2", len(values)), startangle=90,
        counterclock=False, autopct=lambda share: f"{share:.1f}%" if share >= 2 else "",
        pctdistance=0.75, wedgeprops={"edgecolor": "white", "linewidth": 1},
    )
    axes.legend(
        wedges, [f"{name}: {amount:,.0f} GWh" for name, amount in values.items()],
        title="Power source", bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=9,
    )
    axes.grid(False)
    axes.set_aspect("equal")
    return figure
