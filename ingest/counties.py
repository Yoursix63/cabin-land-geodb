"""
Build the counties_in_scope set: VA + WV counties whose drive time from
the project origin (Alexandria, VA) is within DRIVE_TIME_MINUTES.

Source data:
- TIGERweb county polygons (2024) for state FIPS 51 (VA) and 54 (WV)
- OSRM public router for drive-time from origin to county centroid

Outputs:
- data/processed/counties_in_scope.geojson   full polygons (gitignored)
- sql/seeds/counties_in_scope.csv             tabular manifest (committed)

Run:
    python -m ingest.counties
"""
from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

ORIGIN_LAT = float(os.getenv("ORIGIN_LAT", "38.8048"))
ORIGIN_LON = float(os.getenv("ORIGIN_LON", "-77.0469"))
DRIVE_TIME_MAX = int(os.getenv("DRIVE_TIME_MINUTES", "180"))
OSRM_URL = os.getenv("ROUTING_URL") or "https://router.project-osrm.org"

STATE_FIPS = {"51": "VA", "54": "WV"}
TIGER_YEAR = 2024

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "processed"
SEED_DIR = ROOT / "sql" / "seeds"

TIGERWEB_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "State_County/MapServer/13/query"
)


def fetch_counties() -> list[dict]:
    params = {
        "where": "STATE='51' OR STATE='54'",
        "outFields": "GEOID,STATE,COUNTY,NAME,INTPTLAT,INTPTLON",
        "outSR": "4326",
        "f": "geojson",
    }
    r = requests.get(TIGERWEB_URL, params=params, timeout=60)
    r.raise_for_status()
    return r.json()["features"]


def drive_minutes(lon: float, lat: float) -> float | None:
    url = f"{OSRM_URL}/route/v1/driving/{ORIGIN_LON},{ORIGIN_LAT};{lon},{lat}"
    try:
        r = requests.get(url, params={"overview": "false"}, timeout=30)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    data = r.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    return data["routes"][0]["duration"] / 60.0


def main() -> None:
    SEED_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Fetching VA + WV counties from TIGERweb ({TIGER_YEAR})...")
    features = fetch_counties()
    print(f"  got {len(features)} features")

    print(f"Computing drive time from ({ORIGIN_LAT}, {ORIGIN_LON}) via OSRM...")
    print(f"  threshold: {DRIVE_TIME_MAX} minutes")

    in_scope_features: list[dict] = []
    rows: list[dict] = []
    for feat in tqdm(features, unit="county"):
        props = feat["properties"]
        lat = float(props["INTPTLAT"])
        lon = float(props["INTPTLON"])
        minutes = drive_minutes(lon, lat)
        time.sleep(0.05)
        if minutes is None or minutes > DRIVE_TIME_MAX:
            continue
        fips = props["GEOID"]
        state_fips = props["STATE"]
        in_scope_features.append({
            "type": "Feature",
            "geometry": feat["geometry"],
            "properties": {
                **props,
                "drive_minutes": round(minutes, 1),
                "state_abbr": STATE_FIPS[state_fips],
                "tiger_year": TIGER_YEAR,
            },
        })
        rows.append({
            "county_fips": fips,
            "state_fips": state_fips,
            "state_abbr": STATE_FIPS[state_fips],
            "name": props["NAME"],
            "drive_minutes": round(minutes, 1),
            "tiger_year": TIGER_YEAR,
        })

    rows.sort(key=lambda r: (r["state_abbr"], r["name"]))

    geojson_path = DATA_DIR / "counties_in_scope.geojson"
    with geojson_path.open("w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": in_scope_features}, f)

    csv_path = SEED_DIR / "counties_in_scope.csv"
    if rows:
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    print(f"\nIn scope: {len(rows)} counties")
    by_state: dict[str, list[str]] = {}
    for r in rows:
        by_state.setdefault(r["state_abbr"], []).append(
            f"{r['name']} ({r['drive_minutes']:.0f}m)"
        )
    for st, names in sorted(by_state.items()):
        print(f"\n  {st} ({len(names)}):")
        for n in names:
            print(f"    {n}")
    print(f"\nWrote {csv_path.relative_to(ROOT)}")
    print(f"Wrote {geojson_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
