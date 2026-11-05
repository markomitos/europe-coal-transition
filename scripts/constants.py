"""Shared analysis assumptions and source column names.

The reference year anchors asset ages; the observation year selects measured
indicators. Retirement deadlines are fixed calendar years, not relative ages.
"""

REFERENCE_YEAR = 2026
OBSERVATION_YEAR = 2024
HOURS_PER_YEAR = 8760
COAL_CAPACITY_FACTOR = 0.65
HIGH_URGENCY_YEAR = 2030
MEDIUM_URGENCY_YEAR = 2035

ENERGY_COLUMNS = [
    "population", "gdp", "coal_electricity", "electricity_generation",
    "coal_share_elec", "renewables_electricity", "renewables_share_elec",
    "solar_electricity", "wind_electricity", "greenhouse_gas_emissions",
    "carbon_intensity_elec",
]
GENERATION_COLUMNS = [
    "total_gwh", "coal_gwh", "natural_gas_gwh", "oil_gwh", "nuclear_gwh",
    "hydro_gwh", "wind_gwh", "solar_gwh", "renewables_gwh",
]
