<h1 align="center">Origin-Destination Based Traffic Analysis</h1>

<p align="center">
  <strong>Generate road-based OD coordinates and analyze traffic variation using Google Routes API.</strong>
</p>

<p align="center">
  This project starts with road junction coordinates, generates origin and
  destination points around each junction, collects hourly traffic observations,
  and produces cleaned traffic analysis outputs.
</p>

<p align="center">
  <img alt="OD Generation" src="https://img.shields.io/badge/OD-Generation-4c566a?style=for-the-badge">
  <img alt="Google Routes API" src="https://img.shields.io/badge/API-Google%20Routes-4285F4?style=for-the-badge">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.9%2B-3776AB?style=for-the-badge">
  <img alt="Analysis" src="https://img.shields.io/badge/Traffic-Analysis-00b894?style=for-the-badge">
  <img alt="Stack" src="https://img.shields.io/badge/Stack-Pandas%20%2B%20OpenPyXL-2d3436?style=for-the-badge">
</p>

<p align="center">
  <img alt="Libraries" src="https://img.shields.io/badge/Libraries-Used-4c566a?style=for-the-badge">
  <img alt="Requests" src="https://img.shields.io/badge/Requests-2d3436?style=for-the-badge">
  <img alt="Pandas" src="https://img.shields.io/badge/Pandas-150458?style=for-the-badge">
  <img alt="NumPy" src="https://img.shields.io/badge/NumPy-013243?style=for-the-badge">
  <img alt="OpenPyXL" src="https://img.shields.io/badge/OpenPyXL-217346?style=for-the-badge">
  <img alt="Pillow" src="https://img.shields.io/badge/Pillow-8e44ad?style=for-the-badge">
  <img alt="Matplotlib" src="https://img.shields.io/badge/Matplotlib-f39c12?style=for-the-badge">
  <img alt="Seaborn" src="https://img.shields.io/badge/Seaborn-3498db?style=for-the-badge">
</p>

---

## Repository Structure

| File/Folder | Purpose |
| --- | --- |
| `generate_origin_destination.py` | Generates road-based origin and destination coordinates from junction coordinates. |
| `junctions.csv` | Input junction coordinate file. |
| `traffic_collector.py` | Collects traffic observations using the generated OD coordinates. |
| `traffic_hourly_analysis.py` | Cleans raw observations and creates hourly, daily, weekly, and peak-hour summaries. |
| `analysed_data/complete_traffic_analysis.py` | Creates complete analysis outputs, CSV summaries, plots, and workbook-style results. |
| `requirements.txt` | Python dependencies. |
| `.env.example` | Example environment variable file for the Google Maps API key. |

## Python Files

### `generate_origin_destination.py`

Generates origin and destination coordinates for each road junction. It reads
`junctions.csv`, uses the Google Routes API to find a road route through the
junction, and writes points before and after the junction.

Main outputs:

```text
outputs/origin.csv
outputs/destination.csv
outputs/origin_destination.csv
```

The combined `origin_destination.csv` file is used as input for traffic data
collection.

### `traffic_collector.py`

Collects traffic data for each generated origin-destination route. It sends the
origin, junction, and destination coordinates to the Google Routes API and stores
traffic duration, static duration, delay, congestion index, route distance, and
speed values.

Main outputs:

```text
traffic_observations_best_guess.csv
traffic_observations_optimistic.csv
traffic_observations_pessimistic.csv
```

### `traffic_hourly_analysis.py`

Cleans and summarizes the raw traffic observation files. It groups traffic data
by date, hour, traffic model, and junction, then identifies peak traffic hours
using metrics such as congestion index, delay, travel duration, or speed.

Main outputs:

```text
result/
  hourly_traffic_summary_*.csv
  peak_hour_summary_*.csv
  junction_hourly_traffic_summary_*.csv
  junction_peak_hour_summary_*.csv
```

### `analysed_data/complete_traffic_analysis.py`

Performs the complete final traffic analysis. It reads raw traffic data, cleans
important columns, calculates free-flow speed, peak hours, speed reduction,
congestion-delay relationship, weekly comparisons, and creates plots and
workbook-style analysis outputs.

Main outputs:

```text
analysed_data/
  free_flow_speed.csv
  peak_hours_per_junction.csv
  speed_reduction_peak_hours.csv
  congestion_analysis.csv
  weekly_hourly_network_profile.csv
  TRAFFIC_ANALYSIS_FINAL_REPORT.xlsx
  plots/
```

## Complete Workflow

```text
junctions.csv
   |
   v
generate_origin_destination.py
   |
   v
outputs/origin_destination.csv
   |
   v
traffic_collector.py
   |
   v
traffic_observations_*.csv
   |
   v
traffic_hourly_analysis.py
   |
   v
result/ traffic summaries
   |
   v
analysed_data/complete_traffic_analysis.py
```

## Requirements

- Python 3.9 or newer
- Google Maps Platform API key
- Google Routes API enabled in Google Cloud

Install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Set your Google Maps API key:

```powershell
$env:GOOGLE_MAPS_API_KEY="YOUR_API_KEY_HERE"
```

Do not commit your real API key to GitHub.

## 1. Prepare Junction Coordinates

The input file should contain junction IDs and coordinates:

```csv
junction,lat,long
1,9.84754739,78.48282678
2,9.846438744,78.6351097
```

The included sample file is:

```text
junctions.csv
```

A single latitude/longitude point does not define road direction by itself. The
OD generator chooses direction in this order:

1. Uses `front_bearing` and `back_bearing` columns if both exist.
2. Uses `bearing` as the front direction if available.
3. Uses row order in `junctions.csv` if no bearing columns are provided.

Optional bearing format:

```csv
junction,lat,long,bearing
1,9.84754739,78.48282678,15
```

Optional explicit front/back bearing format:

```csv
junction,lat,long,back_bearing,front_bearing
1,9.84754739,78.48282678,195,15
```

## 2. Generate Origin And Destination Points

Run:

```powershell
python .\generate_origin_destination.py --input .\junctions.csv --output-dir .\outputs --distance-km 5
```

This creates:

```text
outputs/origin.csv
outputs/destination.csv
outputs/origin_destination.csv
```

The combined file `outputs/origin_destination.csv` is the input for traffic
collection. It contains:

```csv
junction,junction_lat,junction_long,origin_lat,origin_long,destination_lat,destination_long
```

## 3. Collect Traffic Data

Run one traffic model:

```powershell
python .\traffic_collector.py --junctions .\outputs\origin_destination.csv --traffic-model BEST_GUESS --daily-24h --daily-start-local "2026-09-02T00:00" --output .\traffic_observations_best_guess.csv
```

Google driving departure times must be now or in the future, so use a future
date when collecting a full 24-hour dataset.

Run all three Google traffic models:

```powershell
python .\traffic_collector.py --junctions .\outputs\origin_destination.csv --traffic-model BEST_GUESS --daily-24h --daily-start-local "2026-09-02T00:00" --output .\traffic_observations_best_guess.csv
python .\traffic_collector.py --junctions .\outputs\origin_destination.csv --traffic-model OPTIMISTIC --daily-24h --daily-start-local "2026-09-02T00:00" --output .\traffic_observations_optimistic.csv
python .\traffic_collector.py --junctions .\outputs\origin_destination.csv --traffic-model PESSIMISTIC --daily-24h --daily-start-local "2026-09-02T00:00" --output .\traffic_observations_pessimistic.csv
```

For a quick test:

```powershell
python .\traffic_collector.py --junctions .\outputs\origin_destination.csv --traffic-model BEST_GUESS --limit 1
```

## 4. Clean And Analyze Hourly Traffic

After data collection, create hourly and peak-hour summaries:

```powershell
python .\traffic_hourly_analysis.py --date "2026-09-02"
```

To analyze specific raw files:

```powershell
python .\traffic_hourly_analysis.py --date "2026-09-02" --inputs .\traffic_observations_best_guess.csv .\traffic_observations_optimistic.csv .\traffic_observations_pessimistic.csv
```

Outputs are written under `result/`, for example:

```text
result/
  2026-09-02_hourly_data/
    hourly_traffic_summary_2026-09-02.csv
    peak_hour_summary_2026-09-02.csv
    junction_hourly_traffic_summary_2026-09-02.csv
    junction_peak_hour_summary_2026-09-02.csv
```

## 5. Weekly Analysis

Collect 24-hour data for each date in the week, then run:

```powershell
python .\traffic_hourly_analysis.py --start-date "2026-09-02" --end-date "2026-09-08"
```

This creates weekly network-level and junction-level summaries under `result/`.

## 6. Complete Analysis Workbook And Plots

To create complete analysis outputs from raw traffic data:

```powershell
python .\analysed_data\complete_traffic_analysis.py
```

This script reads raw traffic data, cleans useful columns, creates CSV summaries,
plots, and workbook-style results inside `analysed_data/`.

## Metrics Used

For each successful traffic observation:

```text
delay_s = traffic_duration_s - static_duration_s
congestion_index = traffic_duration_s / static_duration_s
traffic_speed_kmh = (route_distance_m / traffic_duration_s) * 3.6
static_speed_kmh = (route_distance_m / static_duration_s) * 3.6
```

Peak hour is selected using the highest average congestion index by default. If
`traffic_speed_kmh` is used as the peak metric, the lowest average speed is
treated as the peak traffic hour.

## Submitted as part of summer Internship Project 

IIT Kharagpur, RCGSIDM, MUST Lab

Author
```text
Denesh Kumar 
```
