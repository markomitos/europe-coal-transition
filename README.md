# European Coal Phase-Out & Just Transition Tracker

This dashboard explores coal phase-out across Europe, with a focus on the
Balkans. It brings together coal power plant records, electricity generation
data and regional economic indicators to compare the capacity that would need
replacing and the regions that could face greater transition difficulties.

The project uses Python for data processing and calculations, SQLite for storing
and querying the data, and Streamlit for the interface. Pandas and NumPy handle
the tabular data, Matplotlib and Seaborn produce the charts, and PyDeck displays
the map.

## What the app does

The dashboard has four tabs:

- **Overview** shows coal plants on a map, colored by retirement urgency. A
  project's color comes from its most urgent unit.
- **Analysis & SQL** contains charts for unit ages, capacity, electricity
  generation, retirement scenarios and regional indicators. It also includes
  asset records, ownership groups and a console for exploring the database.
- **Just transition** compares regions using a custom vulnerability score and
  shows the contributions behind the highest and lowest scores.
- **Clean replacement** estimates how much solar and wind capacity would be
  needed to replace a selected plant's annual coal generation, along with an
  approximate construction cost.

The sidebar starts with Poland, Serbia and Germany selected. Country selections
filter the analysis, tables and replacement projects. The Overview map keeps all
countries visible for context. The retired-plants toggle applies to both the map
and the analysis. National generation figures stay at country level even when
unit-status filters change.

## Running the project

Python 3.12–3.14 is recommended. From the project folder, run:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/setup_db.py
streamlit run app.py
```

On Windows, activate the environment with `.venv\Scripts\activate` instead.
The dashboard opens at <http://localhost:8501>.

The first setup needs internet access to download the datasets. Downloads are
cached in `data/sources/`, and the database is saved as
`data/coal_transition.sqlite`. Later builds can reuse the cached files. The app
also runs database setup on startup, so it can create a missing database or
rebuild an older schema automatically.

To rebuild the database manually:

```bash
python scripts/setup_db.py --force
```

This still uses the download cache. To download a source again, remove its cached
file before rebuilding. The setup script also accepts `--db`, `--source-dir` and
`--regional-year`. Keep the regional year at its default of 2024 when using the
dashboard, since its national comparisons and regional scores use that year.

## How the project is organized

The code is organized by responsibility. Data imports, database queries and
calculations are kept in separate modules and can run independently of the
dashboard.

```text
coal-transition/
├── app.py                     # Streamlit interface and user controls
├── requirements.txt           # Python dependencies
├── scripts/
│   ├── setup_db.py             # Database schema and import process
│   ├── sources.py              # Downloads, GISCO/ONS regional matching and indicators
│   ├── gem_data.py             # Cleaning and normalizing GEM coal-unit records
│   ├── geography.py            # Country codes, aliases and EU/Non-EU groups
│   ├── queries.py              # SQL queries and the read-only console
│   ├── logic.py                # Retirement, vulnerability and replacement models
│   ├── visualizations.py       # Charts and map legend
│   └── constants.py            # Shared years, capacity factors and column names
└── data/                      # Generated locally; excluded from Git
    ├── sources/               # Cached source downloads
    └── coal_transition.sqlite
```

The data moves through the project in this order:

1. `sources.py` downloads and caches the source files, while `gem_data.py` cleans
   the coal-unit records and keeps the countries and statuses used in the project.
2. Unit coordinates are matched to GISCO NUTS-2 boundaries, with ONS ITL2
   boundaries for the UK. Regional employment and GDP observations are joined
   using exact source region codes; ITL2 codes remain separate from NUTS codes.
3. `setup_db.py` creates the SQLite tables and loads the cleaned data. It checks
   foreign keys and database integrity before replacing an existing database, so
   a failed import leaves the previous database available.
4. `queries.py` reads and aggregates the stored records. `logic.py` calculates
   the retirement scenarios, regional scores and replacement estimates.
5. `app.py` connects the controls to these functions and displays the results
   using `visualizations.py` and the map.

Streamlit caches the data loading and full-fleet retirement calculations. Filters
are applied after classification, so selecting a different country does not
change the peer data used for estimates. Only the selected tab computes its plots.

## Data sources

The project combines seven sources because plant records alone cannot explain
regional economic conditions or actual electricity generation.

| Source | Used for |
|---|---|
| [Global Energy Monitor: Global Coal Plant Tracker, August 2026](https://publicgemdata.nyc3.cdn.digitaloceanspaces.com/interim_maps/gcpt_map_2026-08.geojson) | Coal-unit and project IDs, names, capacity, ownership, coordinates, commissioning years, status and historical closures |
| [Eurostat GISCO NUTS-2 boundaries, 2024](https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/NUTS_RG_20M_2024_4326_LEVL_2.geojson) | Matching unit locations to regions |
| [ONS UK ITL2 boundaries, January 2021, BGC V2](https://www.arcgis.com/home/item.html?id=6750ae0351c749c4b40b31e5740233a0) | Matching UK unit locations to official UK statistical regions, including Northern Ireland |
| [Employment Rates by European Region](https://www.kaggle.com/datasets/rabieelkharoua/employment-rates-by-european-region-1999-2025) | Regional employment rates for people aged 20–64 |
| [Regional GDP per Person in Europe](https://www.kaggle.com/datasets/rabieelkharoua/regional-gdp-per-person-in-europe-2000-2024) | Regional GDP per person and its value relative to the EU27 average |
| [Global Energy, Electricity and Emissions Data](https://www.kaggle.com/datasets/bhavikjikadara/global-energy-electricity-and-emissions-data) | Historical national energy indicators, including coal's share of electricity |
| [Europe Electricity Generation by Fuel](https://www.kaggle.com/datasets/rabieelkharoua/europe-electricity-generation-by-fuel-2008-2026) | Monthly generation by fuel, used for annual generation charts and replacement context |

The GEM source is a public map snapshot with links to the individual GEM wiki
pages. Its statuses describe August 2026, so they are not live operational updates.
The import includes operating, mothballed and retired units; proposed and
under-construction units are outside the scope of this project.

The country scope is the EU27, the six Western Balkan countries, the United
Kingdom, Türkiye, Norway and Switzerland. Imports apply this same scope to units,
regions and national observations. A change to the country scope or boundary
source configuration automatically rebuilds the database on the next startup.

For reference, the November 2026 build from this snapshot contains 1,099 units
across 409 projects in 28 countries: 431 operating, 37 mothballed and 631 retired.
These are units within plants, so a single project can have several records with
different capacities, owners or statuses. The dataset does not cover every small
coal facility.

The regional comparisons use 2024 observations. The two national energy sources
have different units and coverage: the annual energy source retains TWh and
percentage values, while monthly generation is stored in GWh. An annual fuel
total is calculated only when all 12 monthly values are available.

Source URLs, download times, SHA-256 hashes and cleaning reports are saved in
`dataset_metadata` so the imported data can be traced back to its sources.

UK boundaries use native ITL2 `TL…` identifiers. The ONS ArcGIS query requests
WGS84 coordinates (`outSR=4326`) and caches all 41 regions in
`data/sources/ons-itl2-2021-bgc-v2.geojson`. The generalised, coastline-clipped
variant gives the same assignments for the current fleet as the much larger
full-resolution variant: 90 of 92 UK units match. Two units at Methil remain
unmatched in both variants; no nearest-region fallback is used. ONS and Ordnance
Survey attribution and the ONS licence link are retained in source metadata.
These boundaries provide geography; the current indicator sources supply no
compatible 2024 UK GDP or employment observations, so UK scores remain unavailable.

## Database design and SQL

The database separates individual coal units from the information they share,
such as project names, ownership groups and regions.

| Table | Contents |
|---|---|
| `dim_countries` | Country codes, names and EU/Non-EU membership |
| `dim_regions` | Regional keys, country links, employment rates and GDP indicators |
| `dim_owners` | Ownership groups exactly as reported by GEM |
| `dim_plants` | GEM project IDs and project names |
| `fact_units` | Individual units, capacity, status, dates, coordinates and source links |
| `fact_country_energy` | National energy and economic observations by year |
| `fact_monthly_generation` | Electricity generation by country, month and fuel |
| `dataset_metadata` | Source information and build settings |

`fact_units` links to plants, owners and regions through foreign keys, and each
region links to a country. Ownership and location are stored per unit because
units within the same project can have different owners or regional assignments.
Reported ownership groups stay together; the app does not split capacity between
individual companies based on their stakes.

SQL is also part of the coursework. The query catalogue uses joins and grouped
totals for regional capacity, `HAVING` to filter small totals, window functions
for ownership rankings and national capacity shares, and a recursive calendar
with cumulative sums for the phase-out timeline. Filters use bound parameters.

The SQL section lets users select and run predefined analytical queries with the
current sidebar filters and retirement scenario. Queries run read-only, with
limits on execution time and result size.
For scenario queries, modeled retirement dates are applied to a temporary copy
of the units table within the connection. The stored source records stay intact.

## How the calculations work

### Retirement scenarios

The default scenario adds an assumed 45-year lifespan to each non-retired unit's
commissioning year. The slider allows lifespans from 35 to 50 years. Retired units
keep their historical closure dates and are excluded from future retirement
modeling. Mothballed units remain in the capacity scenario, but are excluded from
generation-gap and replacement calculations.

The reference year is fixed at 2026. Scenario dates at or before that year are
classified as overdue, dates through 2030 as high urgency, and 2031–2035 as medium
urgency. Overdue dates are kept as they are. In the timeline, dates before its
2020 start appear as an opening backlog rather than historical closures.

There is an optional fallback for missing commissioning years. It uses the median
observed year from at least five non-retired peers, trying the same country first,
then the EU/Non-EU group, then the full European dataset. The displayed quartiles
show the spread of those peers. All non-retired units in the current snapshot
already have commissioning years, so this fallback is mainly useful for future
data gaps.

These dates are scenario outputs. The imported GEM snapshot provides historical
closures but no future retirement commitments. Turning off modeled dates therefore
leaves no future retirement trajectory for this source.

### Regional vulnerability

The dashboard uses a simple score to compare potential transition difficulties
between regions:

```text
score = 40 × (national coal electricity share / 100)
      + 35 × (1 − regional employment rate / 100)
      + 25 × max(0, 1 − regional GDP EU index / 100)
      + (10 if the region is outside the EU, otherwise 0)
```

The final score is capped at 100. Higher values mean greater potential difficulty
under these assumptions. The weights and the extra non-EU points are choices in
this model, rather than an official measure of transition needs.

National coal share is used as a proxy because regional coal-dependence data is
not available in the imported sources. The employment term is the complement of
the employment rate, not an unemployment rate. The GDP index compares EUR per
person with the EU27 average; it is not adjusted for purchasing power. If any
required observation is missing, the region receives no score.

Regional GDP per person also supplies the vertical axis of the employment/GDP
scatter plot and appears in the unit-level correlation heatmap and data tables.
The separate historical national `gdp` column in `fact_country_energy` is
available in the SQL console; it is not used in vulnerability scores, retirement
scenarios or clean-replacement calculations.

### Solar and wind replacement

The replacement simulator starts with the selected project's operating capacity
and estimates its annual generation using a 65% coal capacity factor. It then
splits that energy between solar and wind using the selected shares:

```text
Coal generation (GWh/year) = coal MW × 8760 × 0.65 / 1000
Replacement capacity (MW) = allocated GWh × 1000 / (8760 × capacity factor)
Construction cost (EUR) = replacement MW × 1000 × cost per kW
```

The assumed capacity factors are 15% for solar and 30% for wind. Default costs are
€700/kW for solar and €1,400/kW for wind, and both can be changed in the app.
The 2030 generation-gap metric uses the same coal-generation assumption for
operating units due to retire by that deadline in the selected scenario.

This calculation matches annual energy only. It leaves out storage, grid upgrades,
curtailment, financing and hourly reliability, so it cannot establish whether the
replacement would supply power whenever it is needed. The national generation pie
provides context; existing renewable generation is not treated as spare output.

## Limits of the project

Missing observations remain missing throughout the import and analysis. Missing
generation values are not filled with zero, national GDP is not substituted for
regional GDP, and regional assignments require a match within a boundary.
GISCO's simplified polygons and ONS's coastline-clipped UK polygons can leave
some coastal or border units without a regional match. These units remain in
the asset dataset.

The generation source groups coal with manufactured gases, and its available
fuel columns do not account for every part of total output. The pie chart puts
the remaining reported generation under **Other / unclassified** and avoids
double counting aggregate renewables.

The imported data also lacks coal-mining employment, awarded transition funding,
unit-level emissions and combustion technology. Those gaps limit what the app
can say about jobs, funding needs and environmental impacts. The dashboard supports
data exploration and comparison of assumptions. Actual transition planning
requires more detailed local research.
