#!/usr/bin/env python3
"""Generate 5 km origin and destination points around junction coordinates.

The script calls Google Routes API for each junction, forces the route through
the junction as a via waypoint, and samples coordinates 5 km before and after
that junction along the returned road polyline.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


EARTH_RADIUS_M = 6_371_000.0
ROUTES_API_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
DEFAULT_FIELD_MASK = "routes.distanceMeters,routes.polyline.encodedPolyline"

Point = Tuple[float, float]


@dataclass(frozen=True)
class Junction:
    identifier: str
    lat: float
    lon: float
    raw: Dict[str, str]


@dataclass(frozen=True)
class Direction:
    back_bearing: float
    front_bearing: float
    source: str


@dataclass(frozen=True)
class RouteSample:
    origin: Point
    destination: Point
    route_distance_m: float
    route_distance_before_junction_m: float
    route_distance_after_junction_m: float
    junction_to_route_m: float
    probe_distance_m: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create origin and destination CSVs with points 5 km behind and "
            "5 km ahead of each junction using Google Routes API."
        )
    )
    parser.add_argument("--input", default="junctions.csv", help="Input junction CSV path.")
    parser.add_argument(
        "--output-dir",
        default="outputs",
        help="Directory where output CSV files will be written.",
    )
    parser.add_argument(
        "--origin-output",
        default="origin.csv",
        help="Origin CSV filename/path. Relative paths are placed inside --output-dir.",
    )
    parser.add_argument(
        "--destination-output",
        default="destination.csv",
        help="Destination CSV filename/path. Relative paths are placed inside --output-dir.",
    )
    parser.add_argument(
        "--combined-output",
        default="origin_destination.csv",
        help="Detailed combined CSV filename/path. Relative paths are placed inside --output-dir.",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("GOOGLE_MAPS_API_KEY"),
        help="Google Maps Platform API key. Defaults to GOOGLE_MAPS_API_KEY env var.",
    )
    parser.add_argument(
        "--distance-km",
        type=float,
        default=5.0,
        help="Distance before and after each junction, in kilometers.",
    )
    parser.add_argument(
        "--probe-distance-km",
        type=float,
        default=None,
        help=(
            "Initial straight-line probe distance used to ask Google for a route. "
            "Defaults to max(distance * 2.5, distance + 2 km)."
        ),
    )
    parser.add_argument(
        "--max-probe-attempts",
        type=int,
        default=3,
        help="Retry count with larger probes when a route is shorter than needed.",
    )
    parser.add_argument(
        "--direction-mode",
        choices=("auto", "sequence", "bearing"),
        default="auto",
        help=(
            "How front/back direction is chosen. auto uses bearing columns if present, "
            "otherwise ordered junction rows."
        ),
    )
    parser.add_argument(
        "--bearing-column",
        default="bearing",
        help="Column containing front bearing in degrees. Back is opposite.",
    )
    parser.add_argument(
        "--front-bearing-column",
        default="front_bearing",
        help="Column containing explicit front bearing in degrees.",
    )
    parser.add_argument(
        "--back-bearing-column",
        default="back_bearing",
        help="Column containing explicit back bearing in degrees.",
    )
    parser.add_argument(
        "--travel-mode",
        choices=("DRIVE", "TWO_WHEELER", "WALK", "BICYCLE"),
        default="DRIVE",
        help="Google Routes API travel mode.",
    )
    parser.add_argument(
        "--routing-preference",
        choices=("TRAFFIC_UNAWARE", "TRAFFIC_AWARE", "TRAFFIC_AWARE_OPTIMAL"),
        default="TRAFFIC_UNAWARE",
        help="Routing preference for DRIVE or TWO_WHEELER requests.",
    )
    parser.add_argument("--avoid-tolls", action="store_true", help="Ask Google to avoid tolls.")
    parser.add_argument(
        "--avoid-highways", action="store_true", help="Ask Google to avoid highways."
    )
    parser.add_argument("--avoid-ferries", action="store_true", help="Ask Google to avoid ferries.")
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.0,
        help="Pause between API calls to reduce quota pressure.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read input and print planned bearings/probes without calling Google API.",
    )
    return parser.parse_args()


def normalize_name(name: str) -> str:
    return name.strip().lower().replace(" ", "").replace("_", "")


def find_column(fieldnames: Sequence[str], candidates: Iterable[str]) -> Optional[str]:
    normalized = {normalize_name(name): name for name in fieldnames}
    for candidate in candidates:
        found = normalized.get(normalize_name(candidate))
        if found:
            return found
    return None


def read_junctions(path: Path) -> Tuple[List[Junction], List[str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no header row.")

        fieldnames = list(reader.fieldnames)
        lat_col = find_column(fieldnames, ("lat", "latitude"))
        lon_col = find_column(fieldnames, ("lon", "lng", "long", "longitude"))
        if not lat_col or not lon_col:
            raise ValueError(
                "Input CSV must contain latitude and longitude columns such as lat,long."
            )

        id_col = find_column(fieldnames, ("junction", "id", "name"))
        if not id_col:
            other_columns = [name for name in fieldnames if name not in {lat_col, lon_col}]
            id_col = other_columns[0] if other_columns else None

        junctions: List[Junction] = []
        for row_number, row in enumerate(reader, start=2):
            try:
                lat = float(row[lat_col])
                lon = float(row[lon_col])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid lat/long at row {row_number}: {row}") from exc

            identifier = (row.get(id_col) if id_col else None) or str(row_number - 1)
            identifier = identifier.strip()
            junctions.append(Junction(identifier=identifier, lat=lat, lon=lon, raw=row))

    if not junctions:
        raise ValueError(f"{path} does not contain any junction rows.")

    return junctions, fieldnames


def has_value(row: Dict[str, str], column: Optional[str]) -> bool:
    return bool(column and row.get(column) not in (None, ""))


def parse_bearing(row: Dict[str, str], column: str) -> float:
    try:
        return float(row[column]) % 360.0
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"Invalid bearing value in column '{column}': {row}") from exc


def initial_bearing(start: Point, end: Point) -> float:
    lat1, lon1 = map(math.radians, start)
    lat2, lon2 = map(math.radians, end)
    delta_lon = lon2 - lon1
    y = math.sin(delta_lon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def destination_point(start: Point, bearing_deg: float, distance_m: float) -> Point:
    lat1 = math.radians(start[0])
    lon1 = math.radians(start[1])
    bearing = math.radians(bearing_deg)
    angular_distance = distance_m / EARTH_RADIUS_M

    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular_distance)
        + math.cos(lat1) * math.sin(angular_distance) * math.cos(bearing)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(angular_distance) * math.cos(lat1),
        math.cos(angular_distance) - math.sin(lat1) * math.sin(lat2),
    )

    normalized_lon = (math.degrees(lon2) + 540.0) % 360.0 - 180.0
    return math.degrees(lat2), normalized_lon


def haversine_m(a: Point, b: Point) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    h = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_M * math.atan2(math.sqrt(h), math.sqrt(1.0 - h))


def cumulative_distances(points: Sequence[Point]) -> List[float]:
    distances = [0.0]
    for previous, current in zip(points, points[1:]):
        distances.append(distances[-1] + haversine_m(previous, current))
    return distances


def local_xy(point: Point, origin: Point) -> Tuple[float, float]:
    lat, lon = point
    origin_lat, origin_lon = origin
    x = (
        EARTH_RADIUS_M
        * math.radians(lon - origin_lon)
        * math.cos(math.radians((lat + origin_lat) / 2.0))
    )
    y = EARTH_RADIUS_M * math.radians(lat - origin_lat)
    return x, y


def point_from_local_xy(x: float, y: float, origin: Point) -> Point:
    origin_lat, origin_lon = origin
    lat = origin_lat + math.degrees(y / EARTH_RADIUS_M)
    lon = origin_lon + math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(origin_lat))))
    return lat, lon


def project_point_onto_route(points: Sequence[Point], target: Point) -> Tuple[float, Point, float]:
    if len(points) < 2:
        raise ValueError("Route polyline has fewer than two points.")

    cumulative = cumulative_distances(points)
    best_distance = float("inf")
    best_route_distance = 0.0
    best_point = points[0]

    for index, (start, end) in enumerate(zip(points, points[1:])):
        start_x, start_y = local_xy(start, target)
        end_x, end_y = local_xy(end, target)
        segment_x = end_x - start_x
        segment_y = end_y - start_y
        segment_len_sq = segment_x * segment_x + segment_y * segment_y
        if segment_len_sq == 0.0:
            fraction = 0.0
        else:
            fraction = max(
                0.0,
                min(1.0, (-(start_x * segment_x + start_y * segment_y)) / segment_len_sq),
            )

        projected_x = start_x + fraction * segment_x
        projected_y = start_y + fraction * segment_y
        distance_to_segment = math.hypot(projected_x, projected_y)
        if distance_to_segment < best_distance:
            segment_distance = haversine_m(start, end)
            best_distance = distance_to_segment
            best_route_distance = cumulative[index] + fraction * segment_distance
            best_point = point_from_local_xy(projected_x, projected_y, target)

    return best_route_distance, best_point, best_distance


def interpolate_route(points: Sequence[Point], distance_m: float) -> Point:
    if distance_m < 0.0:
        raise ValueError("Requested distance is before the start of the route.")

    cumulative = cumulative_distances(points)
    total_distance = cumulative[-1]
    if distance_m > total_distance:
        raise ValueError("Requested distance is after the end of the route.")

    for index in range(len(points) - 1):
        start_distance = cumulative[index]
        end_distance = cumulative[index + 1]
        if distance_m <= end_distance:
            segment_distance = end_distance - start_distance
            if segment_distance == 0.0:
                return points[index]
            fraction = (distance_m - start_distance) / segment_distance
            lat = points[index][0] + fraction * (points[index + 1][0] - points[index][0])
            lon = points[index][1] + fraction * (points[index + 1][1] - points[index][1])
            return lat, lon

    return points[-1]


def decode_polyline(encoded: str) -> List[Point]:
    points: List[Point] = []
    index = 0
    lat = 0
    lon = 0

    while index < len(encoded):
        lat_change, index = decode_polyline_value(encoded, index)
        lon_change, index = decode_polyline_value(encoded, index)
        lat += lat_change
        lon += lon_change
        points.append((lat / 1e5, lon / 1e5))

    return points


def decode_polyline_value(encoded: str, start_index: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    index = start_index

    while True:
        if index >= len(encoded):
            raise ValueError("Invalid encoded polyline.")
        value = ord(encoded[index]) - 63
        index += 1
        result |= (value & 0x1F) << shift
        shift += 5
        if value < 0x20:
            break

    delta = ~(result >> 1) if result & 1 else result >> 1
    return delta, index


def direction_for_junction(
    junctions: Sequence[Junction],
    index: int,
    fieldnames: Sequence[str],
    args: argparse.Namespace,
) -> Direction:
    row = junctions[index].raw
    front_column = find_column(fieldnames, (args.front_bearing_column,))
    back_column = find_column(fieldnames, (args.back_bearing_column,))
    bearing_column = find_column(fieldnames, (args.bearing_column,))

    use_bearing_columns = args.direction_mode in {"auto", "bearing"}
    if use_bearing_columns and has_value(row, front_column) and has_value(row, back_column):
        return Direction(
            back_bearing=parse_bearing(row, back_column or ""),
            front_bearing=parse_bearing(row, front_column or ""),
            source="front_bearing/back_bearing columns",
        )

    if use_bearing_columns and has_value(row, bearing_column):
        front_bearing = parse_bearing(row, bearing_column or "")
        return Direction(
            back_bearing=(front_bearing + 180.0) % 360.0,
            front_bearing=front_bearing,
            source=f"{bearing_column} column",
        )

    if args.direction_mode == "bearing":
        raise ValueError(
            "direction-mode=bearing was selected, but no usable bearing columns were found."
        )

    if len(junctions) < 2:
        raise ValueError(
            "At least two ordered junctions are required when no bearing column is provided."
        )

    current = (junctions[index].lat, junctions[index].lon)
    if index == 0:
        next_point = (junctions[index + 1].lat, junctions[index + 1].lon)
        front_bearing = initial_bearing(current, next_point)
        return Direction(
            back_bearing=(front_bearing + 180.0) % 360.0,
            front_bearing=front_bearing,
            source="CSV row order",
        )

    if index == len(junctions) - 1:
        previous_point = (junctions[index - 1].lat, junctions[index - 1].lon)
        back_bearing = initial_bearing(current, previous_point)
        return Direction(
            back_bearing=back_bearing,
            front_bearing=(back_bearing + 180.0) % 360.0,
            source="CSV row order",
        )

    previous_point = (junctions[index - 1].lat, junctions[index - 1].lon)
    next_point = (junctions[index + 1].lat, junctions[index + 1].lon)
    return Direction(
        back_bearing=initial_bearing(current, previous_point),
        front_bearing=initial_bearing(current, next_point),
        source="CSV row order",
    )


def lat_lng_body(point: Point) -> Dict[str, Any]:
    return {
        "location": {
            "latLng": {
                "latitude": point[0],
                "longitude": point[1],
            }
        }
    }


def build_route_body(
    origin_probe: Point,
    destination_probe: Point,
    junction: Point,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "origin": lat_lng_body(origin_probe),
        "destination": lat_lng_body(destination_probe),
        "intermediates": [
            {
                **lat_lng_body(junction),
                "via": True,
            }
        ],
        "travelMode": args.travel_mode,
        "polylineQuality": "HIGH_QUALITY",
        "polylineEncoding": "ENCODED_POLYLINE",
        "computeAlternativeRoutes": False,
        "units": "METRIC",
    }

    if args.travel_mode in {"DRIVE", "TWO_WHEELER"}:
        body["routingPreference"] = args.routing_preference

    if args.avoid_tolls or args.avoid_highways or args.avoid_ferries:
        body["routeModifiers"] = {
            "avoidTolls": args.avoid_tolls,
            "avoidHighways": args.avoid_highways,
            "avoidFerries": args.avoid_ferries,
        }

    return body


def post_routes_api(body: Dict[str, Any], api_key: str) -> Dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": DEFAULT_FIELD_MASK,
    }
    last_error: Optional[str] = None
    data = json.dumps(body).encode("utf-8")

    for attempt in range(3):
        request = urllib.request.Request(
            ROUTES_API_URL,
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc.read().decode("utf-8", errors="replace")
            if exc.code not in {429, 500, 502, 503, 504}:
                break
        except urllib.error.URLError as exc:
            last_error = str(exc.reason)

        if attempt == 2:
            break
        time.sleep(2.0**attempt)

    raise RuntimeError(f"Google Routes API request failed: {last_error}")


def route_polyline_from_response(payload: Dict[str, Any]) -> Tuple[List[Point], float]:
    routes = payload.get("routes") or []
    if not routes:
        error_text = json.dumps(payload, ensure_ascii=True)
        raise RuntimeError(f"Google Routes API returned no route: {error_text}")

    route = routes[0]
    encoded = ((route.get("polyline") or {}).get("encodedPolyline") or "").strip()
    if not encoded:
        raise RuntimeError(f"Google Routes API route has no encoded polyline: {route}")

    route_distance = float(route.get("distanceMeters") or 0.0)
    return decode_polyline(encoded), route_distance


def sample_route(
    junction: Junction,
    direction: Direction,
    distance_m: float,
    args: argparse.Namespace,
) -> RouteSample:
    junction_point = (junction.lat, junction.lon)
    base_probe_distance_m = (
        args.probe_distance_km * 1000.0
        if args.probe_distance_km is not None
        else max(distance_m * 2.5, distance_m + 2000.0)
    )
    last_error = "No route attempt was made."

    for attempt in range(max(1, args.max_probe_attempts)):
        probe_distance_m = base_probe_distance_m * (2**attempt)
        origin_probe = destination_point(junction_point, direction.back_bearing, probe_distance_m)
        destination_probe = destination_point(
            junction_point, direction.front_bearing, probe_distance_m
        )
        body = build_route_body(origin_probe, destination_probe, junction_point, args)
        payload = post_routes_api(body, args.api_key)
        points, route_distance = route_polyline_from_response(payload)

        route_distance_at_junction, _, junction_to_route_m = project_point_onto_route(
            points, junction_point
        )
        before_m = route_distance_at_junction
        after_m = cumulative_distances(points)[-1] - route_distance_at_junction

        if before_m < distance_m or after_m < distance_m:
            last_error = (
                f"Route around junction is too short for {distance_m:.0f} m offsets "
                f"(before={before_m:.1f} m, after={after_m:.1f} m, "
                f"probe={probe_distance_m:.1f} m)."
            )
            continue

        origin = interpolate_route(points, route_distance_at_junction - distance_m)
        destination = interpolate_route(points, route_distance_at_junction + distance_m)
        return RouteSample(
            origin=origin,
            destination=destination,
            route_distance_m=route_distance,
            route_distance_before_junction_m=before_m,
            route_distance_after_junction_m=after_m,
            junction_to_route_m=junction_to_route_m,
            probe_distance_m=probe_distance_m,
        )

    raise RuntimeError(last_error)


def resolve_output_path(output_dir: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return output_dir / path


def format_coord(value: float) -> str:
    return f"{value:.8f}"


def write_outputs(
    origin_path: Path,
    destination_path: Path,
    combined_path: Path,
    origin_rows: Sequence[Dict[str, str]],
    destination_rows: Sequence[Dict[str, str]],
    combined_rows: Sequence[Dict[str, str]],
) -> None:
    for path in (origin_path, destination_path, combined_path):
        path.parent.mkdir(parents=True, exist_ok=True)

    with origin_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["origin", "lat", "long"])
        writer.writeheader()
        writer.writerows(origin_rows)

    with destination_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["destination", "lat", "long"])
        writer.writeheader()
        writer.writerows(destination_rows)

    combined_fields = [
        "junction",
        "junction_lat",
        "junction_long",
        "origin_lat",
        "origin_long",
        "destination_lat",
        "destination_long",
        "distance_km",
        "back_bearing",
        "front_bearing",
        "direction_source",
        "route_distance_m",
        "route_distance_before_junction_m",
        "route_distance_after_junction_m",
        "junction_to_route_m",
        "probe_distance_m",
        "status",
        "error",
    ]
    with combined_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=combined_fields)
        writer.writeheader()
        writer.writerows(combined_rows)


def print_dry_run(
    junctions: Sequence[Junction],
    fieldnames: Sequence[str],
    args: argparse.Namespace,
    distance_m: float,
) -> None:
    base_probe_distance_m = (
        args.probe_distance_km * 1000.0
        if args.probe_distance_km is not None
        else max(distance_m * 2.5, distance_m + 2000.0)
    )
    for index, junction in enumerate(junctions):
        direction = direction_for_junction(junctions, index, fieldnames, args)
        junction_point = (junction.lat, junction.lon)
        origin_probe = destination_point(
            junction_point, direction.back_bearing, base_probe_distance_m
        )
        destination_probe = destination_point(
            junction_point, direction.front_bearing, base_probe_distance_m
        )
        print(
            f"{junction.identifier}: back={direction.back_bearing:.2f} deg, "
            f"front={direction.front_bearing:.2f} deg, source={direction.source}, "
            f"origin_probe={format_coord(origin_probe[0])},{format_coord(origin_probe[1])}, "
            f"destination_probe={format_coord(destination_probe[0])},{format_coord(destination_probe[1])}"
        )


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    distance_m = args.distance_km * 1000.0

    if distance_m <= 0:
        print("--distance-km must be greater than zero.", file=sys.stderr)
        return 2

    try:
        junctions, fieldnames = read_junctions(input_path)
    except Exception as exc:
        print(f"Could not read junctions: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        try:
            print_dry_run(junctions, fieldnames, args, distance_m)
        except Exception as exc:
            print(f"Dry run failed: {exc}", file=sys.stderr)
            return 2
        return 0

    if not args.api_key:
        print(
            "Missing API key. Set GOOGLE_MAPS_API_KEY or pass --api-key.",
            file=sys.stderr,
        )
        return 2

    origin_rows: List[Dict[str, str]] = []
    destination_rows: List[Dict[str, str]] = []
    combined_rows: List[Dict[str, str]] = []

    for index, junction in enumerate(junctions):
        print(f"[{index + 1}/{len(junctions)}] Processing junction {junction.identifier}...")
        try:
            direction = direction_for_junction(junctions, index, fieldnames, args)
            sample = sample_route(junction, direction, distance_m, args)

            origin_rows.append(
                {
                    "origin": junction.identifier,
                    "lat": format_coord(sample.origin[0]),
                    "long": format_coord(sample.origin[1]),
                }
            )
            destination_rows.append(
                {
                    "destination": junction.identifier,
                    "lat": format_coord(sample.destination[0]),
                    "long": format_coord(sample.destination[1]),
                }
            )
            combined_rows.append(
                {
                    "junction": junction.identifier,
                    "junction_lat": format_coord(junction.lat),
                    "junction_long": format_coord(junction.lon),
                    "origin_lat": format_coord(sample.origin[0]),
                    "origin_long": format_coord(sample.origin[1]),
                    "destination_lat": format_coord(sample.destination[0]),
                    "destination_long": format_coord(sample.destination[1]),
                    "distance_km": f"{args.distance_km:.3f}",
                    "back_bearing": f"{direction.back_bearing:.6f}",
                    "front_bearing": f"{direction.front_bearing:.6f}",
                    "direction_source": direction.source,
                    "route_distance_m": f"{sample.route_distance_m:.1f}",
                    "route_distance_before_junction_m": (
                        f"{sample.route_distance_before_junction_m:.1f}"
                    ),
                    "route_distance_after_junction_m": (
                        f"{sample.route_distance_after_junction_m:.1f}"
                    ),
                    "junction_to_route_m": f"{sample.junction_to_route_m:.1f}",
                    "probe_distance_m": f"{sample.probe_distance_m:.1f}",
                    "status": "ok",
                    "error": "",
                }
            )
        except Exception as exc:
            combined_rows.append(
                {
                    "junction": junction.identifier,
                    "junction_lat": format_coord(junction.lat),
                    "junction_long": format_coord(junction.lon),
                    "origin_lat": "",
                    "origin_long": "",
                    "destination_lat": "",
                    "destination_long": "",
                    "distance_km": f"{args.distance_km:.3f}",
                    "back_bearing": "",
                    "front_bearing": "",
                    "direction_source": "",
                    "route_distance_m": "",
                    "route_distance_before_junction_m": "",
                    "route_distance_after_junction_m": "",
                    "junction_to_route_m": "",
                    "probe_distance_m": "",
                    "status": "error",
                    "error": str(exc),
                }
            )
            print(f"  error: {exc}", file=sys.stderr)

        if args.sleep_seconds > 0 and index < len(junctions) - 1:
            time.sleep(args.sleep_seconds)

    origin_path = resolve_output_path(output_dir, args.origin_output)
    destination_path = resolve_output_path(output_dir, args.destination_output)
    combined_path = resolve_output_path(output_dir, args.combined_output)
    write_outputs(
        origin_path,
        destination_path,
        combined_path,
        origin_rows,
        destination_rows,
        combined_rows,
    )

    ok_count = sum(1 for row in combined_rows if row["status"] == "ok")
    print(f"Done. Successful junctions: {ok_count}/{len(junctions)}")
    print(f"Origin CSV: {origin_path}")
    print(f"Destination CSV: {destination_path}")
    print(f"Detailed CSV: {combined_path}")
    return 0 if ok_count == len(junctions) else 1


if __name__ == "__main__":
    raise SystemExit(main())
