

import argparse
import datetime as dt
import gzip
import json
import os
import re
import time

import requests

URL = "https://archive-api.open-meteo.com/v1/archive"
HEADERS = {"User-Agent": "CS1090A-final-project-SnowPredict/2026 (Harvard student course project)"}

CACHE_DIR = "data/raw/openmeteo"
USAGE_LOG = "data/raw/openmeteo_usage_log.json"
REJECTED_FILE = "data/raw/openmeteo_rejected_variables.json"

# Preliminary range: the last 10 winters. Move START back (e.g. "2004-08-01") for more.
START = "2016-08-01"
END = (dt.date.today() - dt.timedelta(days=7)).isoformat()   # archive lags ~5 days

# One coordinate per resort, three elevation bands
RESORTS = {
    "st_anton":     dict(lat=47.1450, lon=10.2400, tz="Europe/Vienna",
                         bands=dict(base=1304, mid=2100, top=2811)),
    "grandvalira":  dict(lat=42.5550, lon=1.7000,  tz="Europe/Andorra",
                         bands=dict(base=1800, mid=2200, top=2560)),
    "val_di_fassa": dict(lat=46.4800, lon=11.7700, tz="Europe/Rome",
                         bands=dict(base=1465, mid=2200, top=2950)),
}

DAILY = [
    "weather_code",
    "temperature_2m_max", "temperature_2m_min", "temperature_2m_mean",
    "apparent_temperature_max", "apparent_temperature_min", "apparent_temperature_mean",
    "wet_bulb_temperature_2m_max", "wet_bulb_temperature_2m_min", "wet_bulb_temperature_2m_mean",
    "dew_point_2m_max", "dew_point_2m_min", "dew_point_2m_mean",
    "snowfall_sum", "snowfall_water_equivalent_sum",
    "snow_depth_max", "snow_depth_min", "snow_depth_mean",
    "precipitation_sum", "rain_sum", "precipitation_hours",
    "sunshine_duration", "daylight_duration", "sunrise", "sunset",
    "cloud_cover_mean", "cloud_cover_max", "cloud_cover_min", "shortwave_radiation_sum",
    "wind_speed_10m_max", "wind_speed_10m_mean", "wind_speed_10m_min",
    "wind_gusts_10m_max", "wind_gusts_10m_mean", "wind_gusts_10m_min",
    "wind_direction_10m_dominant",
    "relative_humidity_2m_max", "relative_humidity_2m_min", "relative_humidity_2m_mean",
    "pressure_msl_max", "pressure_msl_min", "pressure_msl_mean",
    "surface_pressure_max", "surface_pressure_min", "surface_pressure_mean",
    "vapour_pressure_deficit_max", "et0_fao_evapotranspiration",
    "soil_temperature_0_to_7cm_mean", "soil_temperature_0_to_100cm_mean",
    "soil_moisture_0_to_7cm_mean", "soil_moisture_0_to_100cm_mean",
]

HOURLY = [
    "temperature_2m", "apparent_temperature", "wet_bulb_temperature_2m", "dew_point_2m",
    "relative_humidity_2m", "snowfall", "snow_depth", "snow_depth_water_equivalent",
    "precipitation", "rain", "weather_code", "is_day", "sunshine_duration",
    "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
    "shortwave_radiation", "direct_radiation", "diffuse_radiation", "direct_normal_irradiance",
    "terrestrial_radiation", "wind_speed_10m", "wind_speed_100m", "wind_direction_10m",
    "wind_direction_100m", "wind_gusts_10m", "pressure_msl", "surface_pressure",
    "et0_fao_evapotranspiration", "vapour_pressure_deficit", "boundary_layer_height",
    "total_column_integrated_water_vapour", "albedo",
    "soil_temperature_0_to_7cm", "soil_temperature_7_to_28cm",
    "soil_temperature_28_to_100cm", "soil_temperature_100_to_255cm",
    "soil_moisture_0_to_7cm", "soil_moisture_7_to_28cm",
    "soil_moisture_28_to_100cm", "soil_moisture_100_to_255cm",
]

# (resolution, model, last date the model covers). Priority order: earlier = fetched first.
# best_match = Open-Meteo's blend; era5_land = one consistent ~9 km reanalysis;
# cerra = 5 km European reanalysis (ends mid-2021).
TIERS = [
    ("daily",  "best_match", None),
    ("hourly", "best_match", None),
    ("daily",  "era5_land",  None),
    ("daily",  "cerra",      "2021-06-30"),
]

LIMITS = [(60, 540), (3600, 4800), (86400, 9600)]   # (window s, weighted calls), with margin


class DailyLimit(Exception):
    pass


# For convenience, creates the usage log. Returns default if the file doesn't exist yet so the first run doesn't crash
def load_json(path, default):
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default

# Saves the list of rejected measure names. Creates a folder if needed
def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)

# counts the days between 2 dates. Just for convenience, would end up writing this over and over otherwise
def n_days(start, end):
    return (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1

# works out how many calls Open-Meteo will charge for one request
def weight(n_vars, days):
    return max(1.0, n_vars / 10) * max(1.0, days / 14)

# Sleep until w weighted calls fit under every limit, then log them.
def spend(w):
    while True:
        now = time.time()
        log = [e for e in load_json(USAGE_LOG, []) if e[0] > now - 86400]
        for window, limit in LIMITS:
            recent = sorted(e for e in log if e[0] > now - window)
            used = sum(x for _, x in recent)
            if used + w <= limit:
                continue
            if window == 86400:
                raise DailyLimit(f"{used:,.0f} weighted calls in the last 24h")
            freed, wake = 0.0, now + window          # wait until enough old calls age out
            for t, x in recent:
                freed += x
                if used - freed + w <= limit:
                    wake = t + window
                    break
            print(f"    pacing: sleeping {wake - now + 1:,.0f}s")
            time.sleep(max(1, wake - now + 1))
            break
        else:
            save_json(USAGE_LOG, log + [[now, w]])
            return

# pulls out the reason for Open-Meteo errors
def reason(r):
    try:
        return str(r.json().get("reason", ""))
    except ValueError:
        return r.text[:200]

# Based on sec01. Waits and retries on a network error.
def get_checked(params, tries=5):
    for attempt in range(tries):
        try:
            r = requests.get(URL, params=params, headers=HEADERS, timeout=180)
        except requests.RequestException as e:
            print(f"    network error ({e.__class__.__name__}); retrying")
            time.sleep(10 * 2 ** attempt)
            continue
        if r.status_code in (429, 503):
            why = reason(r).lower()
            if "daily" in why:
                raise DailyLimit(why)
            wait = 3605 - time.time() % 3600 if "hourly" in why else \
                61 if "minutely" in why else min(int(r.headers.get("Retry-After", 10 * 2 ** attempt)), 120)
            print(f"    throttled ({r.status_code}); waiting {wait:.0f}s")
            time.sleep(wait)
            continue
        if r.ok or r.status_code == 400:
            return r
        print(f"    HTTP {r.status_code}: {reason(r)[:120]}")
        return None
    return None


# Similar to looks_real in sec01. Rasies and error if the pulled value has an error flag, is missing the daily or hourly sections, has the wrong number of timestamps, or is missing any of the measures we are looking for
def check_payload(p, res, variables, start, end):
    """sec01 looks_real: raise if this isn't the data we asked for."""
    block = p.get(res)
    if p.get("error") or not isinstance(block, dict):
        raise ValueError(p.get("reason", f"no '{res}' block"))
    expected = n_days(start, end) * (24 if res == "hourly" else 1)
    if len(block.get("time", [])) != expected:
        raise ValueError(f"{len(block.get('time', []))} timestamps, expected {expected}")
    missing = [v for v in variables if v not in block]
    if missing:
        raise ValueError(f"missing variables {missing}")


# Very similar to sec01. Fetches data and caches it. Checks if data is already in cache before pulling again.
def fetch_to_cache(path, resort, band, res, model, start, end, rejected):
    if os.path.isfile(path):
        return "cached"
    key = f"{res}/{model}"
    r_ = RESORTS[resort]
    while True:
        variables = [v for v in (DAILY if res == "daily" else HOURLY) if v not in rejected.get(key, [])]
        params = dict(latitude=r_["lat"], longitude=r_["lon"], elevation=r_["bands"][band],
                      start_date=start, end_date=end, models=model, timezone=r_["tz"],
                      wind_speed_unit="kmh", **{res: ",".join(variables)})
        spend(weight(len(variables), n_days(start, end)))
        r = get_checked(params)
        if r is None:
            return "failed"
        if r.status_code == 400:
            bad = re.findall(r"invalid String value\W*([A-Za-z0-9_]+)", reason(r))
            if not bad or bad[0] not in variables:          # e.g. the model itself is unknown
                print(f"    API rejected request: {reason(r)[:150]}")
                return "rejected"
            print(f"    API doesn't offer '{bad[0]}' for {key}; dropping it")
            rejected.setdefault(key, []).append(bad[0])
            save_json(REJECTED_FILE, rejected)
            continue
        try:
            payload = r.json()
            check_payload(payload, res, variables, start, end)
        except ValueError as e:
            print(f"    not caching: {e}")
            return "failed"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(path + ".tmp", path)                     # never leave a half-written file
        return "fetched"


# Converts any .json.gz files from the older version of this script into plain .json, so they still count as cached and don't get pulled again
def unzip_old_cache():
    for root, _, files in os.walk(CACHE_DIR):
        for name in files:
            if name.endswith(".json.gz"):
                gz = os.path.join(root, name)
                with gzip.open(gz, "rt", encoding="utf-8") as f:
                    payload = json.load(f)
                with open(gz[:-3], "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                os.replace(gz, gz + ".converted")   # keep the original, out of the way


# Builds the full list of every combination of weather model and data type, year, resort and height
def plan():
    """One job per (tier, calendar year, resort, band). The last year's file name carries
    its end date, so moving END later fetches a fresh copy instead of a stale partial one."""
    jobs = []
    for res, model, model_end in TIERS:
        end = min(END, model_end or END)
        s, e = dt.date.fromisoformat(START), dt.date.fromisoformat(end)
        for y in range(s.year, e.year + 1):
            cs, ce = max(s, dt.date(y, 1, 1)).isoformat(), min(e, dt.date(y, 12, 31)).isoformat()
            for resort, r_ in RESORTS.items():
                for band in r_["bands"]:
                    path = f"{CACHE_DIR}/{res}_{model}/{resort}_{band}_{cs}_{ce}.json"
                    jobs.append((path, resort, band, res, model, cs, ce))
    return jobs


# Runs everything
def main():
    global START, END
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print plan and cost; fetch nothing")
    ap.add_argument("--start")
    ap.add_argument("--end")
    args = ap.parse_args()
    START, END = args.start or START, args.end or END

    unzip_old_cache()
    rejected = load_json(REJECTED_FILE, {})
    todo = [j for j in plan() if not os.path.isfile(j[0])]           # per-file guard
    cost = sum(weight(len(DAILY if j[3] == "daily" else HOURLY), n_days(j[5], j[6])) for j in todo)
    print(f"{START}..{END}: {len(plan())} files planned, {len(todo)} to fetch, "
          f"~{cost:,.0f} weighted calls (~{cost / LIMITS[2][1]:.1f} days of free quota)")
    if args.dry_run:
        return

    counts, dead_tiers = {}, set()
    try:
        for i, job in enumerate(todo, 1):
            if job[3:5] in dead_tiers:                   # model/resolution the API refused
                continue
            print(f"[{i}/{len(todo)}] {job[3]}/{job[4]} {job[1]}/{job[2]} {job[5]}..{job[6]}")
            status = fetch_to_cache(*job, rejected)
            counts[status] = counts.get(status, 0) + 1
            if status == "rejected":
                dead_tiers.add(job[3:5])
    except DailyLimit as e:
        print(f"\nDaily API limit reached ({e}). Cache is intact -- run again in ~24h to continue.")
    print("summary:", counts)


if __name__ == "__main__":
    main()
