import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

FIELDNAMES = [
    "observed_at_utc",
    "observed_at_local",
    "requested_departure_utc",
    "requested_departure_local",
    "source_row",
    "junction_name",
    "junction_latitude",
    "junction_longitude",
    "sample_direction",
    "origin_latitude",
    "origin_longitude",
    "destination_latitude",
    "destination_longitude",
    "route_distance_m",
    "no_traffic_route_distance_m",
    "route_distance_delta_m",
    "traffic_duration_s",
    "static_duration_s",
    "no_traffic_route_duration_s",
    "delay_s",
    "congestion_index",
    "traffic_speed_kmh",
    "static_speed_kmh",
    "no_traffic_route_api_status",
    "api_status",
    "notes",
]

STRETCH_DIRECTION = "origin_to_destination"

# Maps CLI model name → a safe filename suffix
MODEL_FILE_SUFFIX = {
    "BEST_GUESS":  "best_guess",
    "OPTIMISTIC":  "optimistic",
    "PESSIMISTIC": "pessimistic",
}


def parse_float(value):
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_coordinate_pair(value):
    if value is None:
        return None
    parts = [part.strip() for part in str(value).split(",")]
    if len(parts) != 2:
        return None

    lat = parse_float(parts[0])
    lng = parse_float(parts[1])
    if lat is None or lng is None:
        return None
    return lat, lng


def first_nonblank(row, keys):
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def coordinate_from_columns(row, latitude_keys, longitude_keys):
    lat = parse_float(first_nonblank(row, latitude_keys))
    lng = parse_float(first_nonblank(row, longitude_keys))
    if lat is None or lng is None:
        return None
    return lat, lng


def parse_duration_seconds(value):
    if not value:
        return None
    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)s$", value)
    if not match:
        return None
    return float(match.group(1))


def format_utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def format_local(value):
    return value.astimezone().isoformat(timespec="seconds")


def parse_local_datetime(value):
    normalized = str(value).strip().replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Use an ISO datetime like 2026-05-15T00:00 or 2026-05-15 00:00"
        ) from exc

    return parsed.astimezone()


def next_full_local_hour(now_utc):
    now_local = now_utc.astimezone()
    hour_start = now_local.replace(minute=0, second=0, microsecond=0)
    if now_local == hour_start:
        return hour_start
    return hour_start + timedelta(hours=1)


def departure_schedule(args, now_utc):
    if not args.daily_24h:
        return [None]

    start_local = args.daily_start_local or next_full_local_hour(now_utc)
    return [start_local + timedelta(hours=hour) for hour in range(24)]


def response_error_message(response):
    try:
        payload = response.json()
    except ValueError:
        text = re.sub(r"<[^>]+>", " ", response.text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:300] + ("..." if len(text) > 300 else "")

    error = payload.get("error", {})
    if isinstance(error, dict):
        message = error.get("message", "")
        status = error.get("status", "")
        if status and message:
            return f"{status}: {message}"
        return message or str(error)

    return payload.get("error_message", "") or str(payload)


def raise_for_google_error(response):
    if response.ok:
        return
    raise requests.HTTPError(
        f"{response.status_code} {response.reason}: {response_error_message(response)}",
        response=response,
    )


def junction_coordinates(row):
    coords = coordinate_from_columns(
        row,
        ["junction_latitude", "junction_lat"],
        ["junction_longitude", "junction_long", "junction_lng", "junction_lon"],
    )
    if coords is not None:
        return coords
    return parse_coordinate_pair(row.get("junction"))


def named_coordinate_pair(row, pair_key, latitude_keys, longitude_keys):
    return coordinate_from_columns(
        row,
        latitude_keys,
        longitude_keys,
    ) or parse_coordinate_pair(row.get(pair_key))


def stretch_points(row):
    origin = named_coordinate_pair(
        row,
        "origin",
        ["origin_latitude", "origin_lat"],
        ["origin_longitude", "origin_long", "origin_lng", "origin_lon"],
    )
    destination = named_coordinate_pair(
        row,
        "destination",
        ["destination_latitude", "destination_lat"],
        [
            "destination_longitude",
            "destination_long",
            "destination_lng",
            "destination_lon",
        ],
    )

    if origin is None or destination is None:
        raise ValueError("Missing coordinate pair for origin or destination")

    origin_lat, origin_lng = origin
    destination_lat, destination_lng = destination
    return origin_lat, origin_lng, destination_lat, destination_lng


def waypoint(lat, lng):
    return {
        "location": {
            "latLng": {
                "latitude": lat,
                "longitude": lng,
            }
        }
    }


def junction_label(row):
    explicit_name = first_nonblank(row, ["junction_name", "junction_id"])
    if explicit_name:
        return explicit_name

    junction = first_nonblank(row, ["junction"])
    if junction and parse_coordinate_pair(junction) is None:
        return junction
    return ""


def compute_route(
    session,
    api_key,
    origin_lat,
    origin_lng,
    junction_lat,
    junction_lng,
    destination_lat,
    destination_lng,
    routing_preference,
    traffic_model=None,
    departure_time=None,
):
    body = {
        "origin": waypoint(origin_lat, origin_lng),
        "destination": waypoint(destination_lat, destination_lng),
        "intermediates": [
            {
                "via": True,
                **waypoint(junction_lat, junction_lng),
            }
        ],
        "travelMode": "DRIVE",
        "routingPreference": routing_preference,
        "languageCode": "en",
        "regionCode": "IN",
        "units": "METRIC",
    }
    if traffic_model is not None:
        body["trafficModel"] = traffic_model
    if departure_time is not None:
        body["departureTime"] = format_utc(departure_time)

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "routes.distanceMeters,routes.duration,routes.staticDuration",
    }

    response = session.post(ROUTES_URL, json=body, headers=headers, timeout=45)
    raise_for_google_error(response)
    payload = response.json()
    routes = payload.get("routes", [])
    if not routes:
        return None
    return routes[0]


def compute_route_traffic(
    session,
    api_key,
    origin_lat,
    origin_lng,
    junction_lat,
    junction_lng,
    destination_lat,
    destination_lng,
    traffic_model,
    departure_time=None,
):
    return compute_route(
        session=session,
        api_key=api_key,
        origin_lat=origin_lat,
        origin_lng=origin_lng,
        junction_lat=junction_lat,
        junction_lng=junction_lng,
        destination_lat=destination_lat,
        destination_lng=destination_lng,
        routing_preference="TRAFFIC_AWARE_OPTIMAL",
        traffic_model=traffic_model,
        departure_time=departure_time,
    )


def compute_route_no_traffic(
    session,
    api_key,
    origin_lat,
    origin_lng,
    junction_lat,
    junction_lng,
    destination_lat,
    destination_lng,
):
    return compute_route(
        session=session,
        api_key=api_key,
        origin_lat=origin_lat,
        origin_lng=origin_lng,
        junction_lat=junction_lat,
        junction_lng=junction_lng,
        destination_lat=destination_lat,
        destination_lng=destination_lng,
        routing_preference="TRAFFIC_UNAWARE",
    )


def load_junctions(path):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    for index, row in enumerate(rows, start=2):
        row.setdefault("source_row", str(index))
    return rows


def row_key(row, precision):
    junction = junction_coordinates(row)
    try:
        origin_lat, origin_lng, destination_lat, destination_lng = stretch_points(row)
    except ValueError:
        return None
    origin = (origin_lat, origin_lng)
    destination = (destination_lat, destination_lng)
    if origin is None or junction is None or destination is None:
        return None

    return (
        round(origin[0], precision),
        round(origin[1], precision),
        round(junction[0], precision),
        round(junction[1], precision),
        round(destination[0], precision),
        round(destination[1], precision),
    )


def valid_junction(row):
    api_status = row.get("api_status", "OK")
    if api_status and api_status != "OK":
        return False

    return row_key(row, 8) is not None


def dedupe_junctions(rows, precision):
    seen = set()
    deduped = []
    for row in rows:
        if not valid_junction(row):
            continue
        key = row_key(row, precision)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def append_rows(path, rows):
    exists = path.exists()
    write_header = not exists
    if exists:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle)
            existing_header = next(reader, None)
        if not existing_header:
            write_header = True
        elif existing_header != FIELDNAMES:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                existing_rows = list(csv.DictReader(handle))
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=FIELDNAMES,
                    extrasaction="ignore",
                )
                writer.writeheader()
                writer.writerows(existing_rows)

    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def build_observation(
    now_utc,
    row,
    direction_name,
    origin_lat,
    origin_lng,
    destination_lat,
    destination_lng,
    route=None,
    no_traffic_route=None,
    no_traffic_route_status="",
    status="OK",
    notes="",
    departure_time=None,
):
    junction_coords = junction_coordinates(row)
    junction_lat, junction_lng = junction_coords if junction_coords else ("", "")
    distance_m = route.get("distanceMeters") if route else None
    no_traffic_distance_m = (
        no_traffic_route.get("distanceMeters") if no_traffic_route else None
    )
    traffic_duration_s = parse_duration_seconds(route.get("duration")) if route else None
    static_duration_s = (
        parse_duration_seconds(route.get("staticDuration")) if route else None
    )
    no_traffic_duration_s = None
    if no_traffic_route:
        no_traffic_duration_s = parse_duration_seconds(
            no_traffic_route.get("duration")
        ) or parse_duration_seconds(no_traffic_route.get("staticDuration"))

    delay_s = None
    congestion_index = None
    traffic_speed_kmh = None
    static_speed_kmh = None
    route_distance_delta_m = None

    if traffic_duration_s is not None and static_duration_s:
        delay_s = traffic_duration_s - static_duration_s
        congestion_index = traffic_duration_s / static_duration_s

    if distance_m is not None and no_traffic_distance_m is not None:
        route_distance_delta_m = distance_m - no_traffic_distance_m

    if distance_m and traffic_duration_s:
        traffic_speed_kmh = (distance_m / traffic_duration_s) * 3.6
    if distance_m and static_duration_s:
        static_speed_kmh = (distance_m / static_duration_s) * 3.6

    return {
        "observed_at_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "observed_at_local": now_utc.astimezone().isoformat(timespec="seconds"),
        "requested_departure_utc": format_utc(departure_time)
        if departure_time is not None
        else "",
        "requested_departure_local": format_local(departure_time)
        if departure_time is not None
        else "",
        "source_row": row.get("source_row", ""),
        "junction_name": junction_label(row),
        "junction_latitude": row.get("junction_latitude", "")
        or row.get("junction_lat", "")
        or (round(junction_lat, 8) if junction_lat != "" else ""),
        "junction_longitude": row.get("junction_longitude", "")
        or row.get("junction_long", "")
        or (round(junction_lng, 8) if junction_lng != "" else ""),
        "sample_direction": direction_name,
        "origin_latitude": round(origin_lat, 8),
        "origin_longitude": round(origin_lng, 8),
        "destination_latitude": round(destination_lat, 8),
        "destination_longitude": round(destination_lng, 8),
        "route_distance_m": distance_m or "",
        "no_traffic_route_distance_m": no_traffic_distance_m or "",
        "route_distance_delta_m": route_distance_delta_m
        if route_distance_delta_m is not None
        else "",
        "traffic_duration_s": round(traffic_duration_s, 2)
        if traffic_duration_s is not None
        else "",
        "static_duration_s": round(static_duration_s, 2)
        if static_duration_s is not None
        else "",
        "no_traffic_route_duration_s": round(no_traffic_duration_s, 2)
        if no_traffic_duration_s is not None
        else "",
        "delay_s": round(delay_s, 2) if delay_s is not None else "",
        "congestion_index": round(congestion_index, 4)
        if congestion_index is not None
        else "",
        "traffic_speed_kmh": round(traffic_speed_kmh, 2)
        if traffic_speed_kmh is not None
        else "",
        "static_speed_kmh": round(static_speed_kmh, 2)
        if static_speed_kmh is not None
        else "",
        "no_traffic_route_api_status": no_traffic_route_status,
        "api_status": status,
        "notes": notes,
    }


def default_output_path(traffic_model):
    """Return a model-specific default output CSV path."""
    suffix = MODEL_FILE_SUFFIX.get(traffic_model, traffic_model.lower())
    return Path(f"traffic_observations_{suffix}.csv")


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Collect one Google Routes API traffic variation observation for each "
            "origin-to-destination stretch, with the junction forced as the middle "
            "via point. Run repeatedly over a week to build a time series."
        )
    )
    parser.add_argument(
        "--junctions",
        default=Path("coordinates_for_traffic_variance.csv"),
        type=Path,
        help=(
            "CSV containing origin, junction, and destination coordinates. Supports "
            "split columns like origin_lat/origin_long/junction_lat/junction_long/"
            "destination_lat/destination_long or combined 'lat,lng' pair columns."
        ),
    )
    parser.add_argument(
        "--output",
        default=None,           # ← changed: None means "derive from model"
        type=Path,
        help=(
            "CSV to append traffic observations into. "
            "Defaults to traffic_observations_<model>.csv "
            "(e.g. traffic_observations_best_guess.csv)."
        ),
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("GOOGLE_MAPS_API_KEY"),
        help="Google Maps API key. Prefer GOOGLE_MAPS_API_KEY environment variable.",
    )
    parser.add_argument(
        "--traffic-model",
        default="BEST_GUESS",
        choices=["BEST_GUESS", "OPTIMISTIC", "PESSIMISTIC"],
        help="Google Routes traffic model to use (default: BEST_GUESS).",
    )
    parser.add_argument(
        "--daily-24h",
        action="store_true",
        help=(
            "Collect 24 hourly traffic estimates by setting departureTime for "
            "each hour. Defaults to the next full local hour."
        ),
    )
    parser.add_argument(
        "--daily-start-local",
        type=parse_local_datetime,
        help=(
            "Local start datetime for --daily-24h, for example "
            "2026-05-15T00:00. Google driving departure times must be now or future."
        ),
    )
    parser.add_argument(
        "--compare-no-traffic-route",
        action="store_true",
        help=(
            "Also request a separate TRAFFIC_UNAWARE route and write its distance "
            "and duration. This adds one extra API call per unique junction."
        ),
    )
    parser.add_argument(
        "--dedupe-round",
        type=int,
        default=6,
        help="Round junction coordinates to this precision before de-duplicating.",
    )
    parser.add_argument("--limit", type=int, help="Only process first N unique junctions")
    parser.add_argument("--sleep", type=float, default=0.1, help="Delay between API calls")
    return parser.parse_args()


def route_cache_key(
    origin_lat,
    origin_lng,
    junction_lat,
    junction_lng,
    destination_lat,
    destination_lng,
):
    return (
        round(origin_lat, 8),
        round(origin_lng, 8),
        round(junction_lat, 8),
        round(junction_lng, 8),
        round(destination_lat, 8),
        round(destination_lng, 8),
    )


def no_traffic_route_result(
    cache,
    session,
    api_key,
    origin_lat,
    origin_lng,
    junction_lat,
    junction_lng,
    destination_lat,
    destination_lng,
):
    key = route_cache_key(
        origin_lat,
        origin_lng,
        junction_lat,
        junction_lng,
        destination_lat,
        destination_lng,
    )
    if key in cache:
        return cache[key]

    try:
        route = compute_route_no_traffic(
            session=session,
            api_key=api_key,
            origin_lat=origin_lat,
            origin_lng=origin_lng,
            junction_lat=junction_lat,
            junction_lng=junction_lng,
            destination_lat=destination_lat,
            destination_lng=destination_lng,
        )
        result = (
            route,
            "OK" if route else "ZERO_RESULTS",
            "" if route else "No no-traffic route returned.",
        )
    except requests.RequestException as exc:
        result = (None, "REQUEST_FAILED", str(exc))

    cache[key] = result
    return result


def main():
    args = parse_args()

    # ── Resolve output path: explicit flag wins, otherwise derive from model ──
    output_path = args.output or default_output_path(args.traffic_model)

    if not args.api_key:
        print(
            "Missing Google Maps API key. In PowerShell, run:\n"
            '$env:GOOGLE_MAPS_API_KEY="your-api-key"\n',
            file=sys.stderr,
        )
        return 2

    rows = load_junctions(args.junctions)
    junctions = dedupe_junctions(rows, args.dedupe_round)
    if args.limit:
        junctions = junctions[: args.limit]

    if not junctions:
        print(
            "No valid junction rows found. Make sure the CSV has origin, junction, "
            "and destination coordinates.",
            file=sys.stderr,
        )
        return 1

    print(
        f"Traffic model : {args.traffic_model}\n"
        f"Output file   : {output_path}\n"
        f"Daily 24h     : {args.daily_24h}\n"
        f"Junctions     : {len(junctions)}"
    )

    session = requests.Session()
    now_utc = datetime.now(timezone.utc)
    departures = departure_schedule(args, now_utc)
    first_departure = departures[0]
    if (
        first_departure is not None
        and first_departure.astimezone(timezone.utc) < now_utc - timedelta(minutes=1)
    ):
        print(
            "--daily-start-local is in the past. For driving routes, Google requires "
            "departureTime to be now or future. Choose a future start time.",
            file=sys.stderr,
        )
        return 2

    observations = []
    total_calls = len(junctions) * len(departures)
    call_index = 0
    no_traffic_route_cache = {}

    for departure_time in departures:
        for row in junctions:
            junction_lat, junction_lng = junction_coordinates(row)
            origin_lat, origin_lng, destination_lat, destination_lng = stretch_points(row)
            call_index += 1
            no_traffic_route = None
            no_traffic_route_status = ""
            no_traffic_route_notes = ""

            try:
                route = compute_route_traffic(
                    session=session,
                    api_key=args.api_key,
                    origin_lat=origin_lat,
                    origin_lng=origin_lng,
                    junction_lat=junction_lat,
                    junction_lng=junction_lng,
                    destination_lat=destination_lat,
                    destination_lng=destination_lng,
                    traffic_model=args.traffic_model,
                    departure_time=departure_time,
                )
                if args.compare_no_traffic_route:
                    (
                        no_traffic_route,
                        no_traffic_route_status,
                        no_traffic_route_notes,
                    ) = no_traffic_route_result(
                        cache=no_traffic_route_cache,
                        session=session,
                        api_key=args.api_key,
                        origin_lat=origin_lat,
                        origin_lng=origin_lng,
                        junction_lat=junction_lat,
                        junction_lng=junction_lng,
                        destination_lat=destination_lat,
                        destination_lng=destination_lng,
                    )

                if route:
                    notes = (
                        f"No-traffic route: {no_traffic_route_notes}"
                        if no_traffic_route_notes
                        else ""
                    )
                    observation = build_observation(
                        now_utc,
                        row,
                        STRETCH_DIRECTION,
                        origin_lat,
                        origin_lng,
                        destination_lat,
                        destination_lng,
                        route=route,
                        no_traffic_route=no_traffic_route,
                        no_traffic_route_status=no_traffic_route_status,
                        notes=notes,
                        departure_time=departure_time,
                    )
                else:
                    notes = "Routes API returned no route."
                    if no_traffic_route_notes:
                        notes = f"{notes} No-traffic route: {no_traffic_route_notes}"
                    observation = build_observation(
                        now_utc,
                        row,
                        STRETCH_DIRECTION,
                        origin_lat,
                        origin_lng,
                        destination_lat,
                        destination_lng,
                        no_traffic_route=no_traffic_route,
                        no_traffic_route_status=no_traffic_route_status,
                        status="ZERO_RESULTS",
                        notes=notes,
                        departure_time=departure_time,
                    )
            except requests.RequestException as exc:
                observation = build_observation(
                    now_utc,
                    row,
                    STRETCH_DIRECTION,
                    origin_lat,
                    origin_lng,
                    destination_lat,
                    destination_lng,
                    status="REQUEST_FAILED",
                    notes=str(exc),
                    departure_time=departure_time,
                )

            observations.append(observation)
            departure_label = (
                format_local(departure_time) if departure_time is not None else "now"
            )
            print(
                f"{call_index}/{total_calls} {junction_label(row)} "
                f"{STRETCH_DIRECTION} departure={departure_label} "
                f"-> {observation['api_status']} CI={observation['congestion_index']}"
            )

            if args.sleep and call_index < total_calls:
                time.sleep(args.sleep)

    append_rows(output_path, observations)
    print(f"\nAppended {len(observations)} observations to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
