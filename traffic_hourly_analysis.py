import argparse
import csv
import glob
import math
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path


HOURLY_FIELDNAMES = [
    "model",
    "date",
    "hour",
    "records",
    "avg_traffic_duration_s",
    "avg_static_duration_s",
    "avg_delay_s",
    "avg_congestion_index",
    "avg_traffic_speed_kmh",
    "min_traffic_speed_kmh",
    "max_traffic_duration_s",
]

JUNCTION_FIELDNAMES = [
    "source_row",
    "junction_name",
    "junction_latitude",
    "junction_longitude",
]

JUNCTION_HOURLY_FIELDNAMES = [
    "model",
    "date",
    *JUNCTION_FIELDNAMES,
    "hour",
    "records",
    "avg_traffic_duration_s",
    "avg_static_duration_s",
    "avg_delay_s",
    "avg_congestion_index",
    "avg_traffic_speed_kmh",
    "min_traffic_speed_kmh",
    "max_traffic_duration_s",
]

PEAK_FIELDNAMES = [
    "model",
    "date",
    "peak_metric",
    "peak_rule",
    "peak_hour",
    "peak_metric_value",
    "records",
    "avg_traffic_duration_s",
    "avg_delay_s",
    "avg_congestion_index",
    "avg_traffic_speed_kmh",
]

JUNCTION_PEAK_FIELDNAMES = [
    "model",
    "date",
    *JUNCTION_FIELDNAMES,
    "peak_metric",
    "peak_rule",
    "peak_hour",
    "peak_metric_value",
    "records",
    "avg_traffic_duration_s",
    "avg_delay_s",
    "avg_congestion_index",
    "avg_traffic_speed_kmh",
]

METRIC_FIELDS = [
    "traffic_duration_s",
    "static_duration_s",
    "delay_s",
    "congestion_index",
    "traffic_speed_kmh",
]

METRIC_TO_SUMMARY_FIELD = {
    "congestion_index": "avg_congestion_index",
    "delay_s": "avg_delay_s",
    "traffic_duration_s": "avg_traffic_duration_s",
    "traffic_speed_kmh": "avg_traffic_speed_kmh",
}

RESULT_DIR = Path("result")
SKIP_INPUT_DIRS = {"result", "results", "old result", "__pycache__"}


def parse_float(value):
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def parse_datetime(value):
    value = str(value or "").strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_date(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Use an ISO date like 2026-05-21"
        ) from exc


def model_from_path(path):
    match = re.search(r"traffic_observations_(.+)\.csv$", Path(path).name)
    if not match:
        return Path(path).stem
    return match.group(1).upper()


def should_skip_input_path(path):
    return any(part.lower() in SKIP_INPUT_DIRS for part in Path(path).parts)


def discover_input_paths():
    paths = [
        path
        for path in Path(".").rglob("*traffic_observations_*.csv")
        if path.is_file() and not should_skip_input_path(path)
    ]
    return sorted(paths)


def avg(values):
    values = [value for value in values if value is not None]
    if not values:
        return None
    return sum(values) / len(values)


def rounded(value, digits=4):
    if value is None:
        return ""
    return round(value, digits)


def natural_sort_value(value):
    value = clean(value)
    try:
        return 0, float(value)
    except ValueError:
        return 1, value


def clean(value):
    return str(value or "").strip()


def junction_metadata(row):
    return {
        "source_row": clean(row.get("source_row")),
        "junction_name": clean(row.get("junction_name")),
        "junction_latitude": clean(row.get("junction_latitude")),
        "junction_longitude": clean(row.get("junction_longitude")),
    }


def junction_key(row):
    metadata = junction_metadata(row)
    return tuple(metadata[field] for field in JUNCTION_FIELDNAMES)


def date_in_scope(row_date, exact_date=None, start_date=None, end_date=None):
    if exact_date and row_date != exact_date:
        return False
    if start_date and row_date < start_date:
        return False
    if end_date and row_date > end_date:
        return False
    return True


def load_rows(paths, exact_date=None, start_date=None, end_date=None):
    for path in paths:
        model = model_from_path(path)
        with Path(path).open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if row.get("api_status", "OK") != "OK":
                    continue

                requested_dt = parse_datetime(row.get("requested_departure_local"))
                observed_dt = parse_datetime(row.get("observed_at_local"))
                timestamp = requested_dt or observed_dt
                if timestamp is None:
                    continue

                row_date = timestamp.date()
                if not date_in_scope(row_date, exact_date, start_date, end_date):
                    continue

                date = row_date.isoformat()
                yield model, date, timestamp.hour, row


def new_metric_bucket():
    return {field: [] for field in METRIC_FIELDS}


def build_hourly_record(model, date, hour, values, metadata=None):
    traffic_duration = [
        value for value in values["traffic_duration_s"] if value is not None
    ]
    traffic_speed = [
        value for value in values["traffic_speed_kmh"] if value is not None
    ]

    record = {
        "model": model,
        "date": date,
        "hour": f"{hour:02d}:00",
        "records": len(traffic_duration),
        "avg_traffic_duration_s": rounded(avg(traffic_duration), 2),
        "avg_static_duration_s": rounded(avg(values["static_duration_s"]), 2),
        "avg_delay_s": rounded(avg(values["delay_s"]), 2),
        "avg_congestion_index": rounded(avg(values["congestion_index"]), 4),
        "avg_traffic_speed_kmh": rounded(avg(traffic_speed), 2),
        "min_traffic_speed_kmh": rounded(min(traffic_speed), 2)
        if traffic_speed
        else "",
        "max_traffic_duration_s": rounded(max(traffic_duration), 2)
        if traffic_duration
        else "",
    }
    if metadata:
        record.update(metadata)
    return record


def hourly_sort_key(row):
    base = (row["model"], row["date"])
    hour = row.get("hour") or row.get("peak_hour") or ""
    if "junction_name" in row:
        junction = row.get("junction_name") or row.get("source_row")
        return base + (natural_sort_value(junction), hour)
    return base + (hour,)


def summarize_hourly(rows, by_junction=False, date_label=None):
    groups = defaultdict(new_metric_bucket)
    metadata_by_key = {}

    for model, date, hour, row in rows:
        group_date = date_label or date
        if by_junction:
            key = (model, group_date, junction_key(row), hour)
            metadata_by_key[key] = junction_metadata(row)
        else:
            key = (model, group_date, hour)

        group = groups[key]
        for field in group:
            group[field].append(parse_float(row.get(field)))

    summary_rows = []
    for key, values in sorted(groups.items()):
        if by_junction:
            model, date, _junction, hour = key
            metadata = metadata_by_key[key]
        else:
            model, date, hour = key
            metadata = None

        summary_rows.append(
            build_hourly_record(model, date, hour, values, metadata=metadata)
        )
    return sorted(summary_rows, key=hourly_sort_key)


def peak_group_key(row, by_junction):
    if by_junction:
        return (
            row["model"],
            row["date"],
            *(row[field] for field in JUNCTION_FIELDNAMES),
        )
    return row["model"], row["date"]


def peak_rows(hourly_rows, metric, by_junction=False):
    summary_field = METRIC_TO_SUMMARY_FIELD[metric]
    lower_is_worse = metric == "traffic_speed_kmh"
    rule = "lowest average speed" if lower_is_worse else f"highest average {metric}"

    groups = defaultdict(list)
    for row in hourly_rows:
        groups[peak_group_key(row, by_junction)].append(row)

    peak_summary = []
    for rows in groups.values():
        rows_with_metric = [
            row for row in rows if parse_float(row.get(summary_field)) is not None
        ]
        if not rows_with_metric:
            continue
        key = lambda row: parse_float(row[summary_field])
        peak = (
            min(rows_with_metric, key=key)
            if lower_is_worse
            else max(rows_with_metric, key=key)
        )

        row = {
            "model": peak["model"],
            "date": peak["date"],
            "peak_metric": metric,
            "peak_rule": rule,
            "peak_hour": peak["hour"],
            "peak_metric_value": peak[summary_field],
            "records": peak["records"],
            "avg_traffic_duration_s": peak["avg_traffic_duration_s"],
            "avg_delay_s": peak["avg_delay_s"],
            "avg_congestion_index": peak["avg_congestion_index"],
            "avg_traffic_speed_kmh": peak["avg_traffic_speed_kmh"],
        }
        if by_junction:
            row.update({field: peak[field] for field in JUNCTION_FIELDNAMES})
        peak_summary.append(row)
    return sorted(peak_summary, key=hourly_sort_key)


def write_csv(path, fieldnames, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def output_date_suffix(args):
    if args.date:
        return args.date.isoformat()

    if args.start_date or args.end_date:
        start_label = args.start_date.isoformat() if args.start_date else "first"
        end_label = args.end_date.isoformat() if args.end_date else "last"
        return f"{start_label}_to_{end_label}"

    return "all_dates"


def dated_csv_name(base_name, suffix):
    return f"{base_name}_{suffix}.csv"


def output_folder(args):
    suffix = output_date_suffix(args)
    if args.start_date or args.end_date:
        return RESULT_DIR / f"{suffix}_weekly_data"
    return RESULT_DIR / f"{suffix}_hourly_data"


def resolve_output_paths(args):
    suffix = output_date_suffix(args)
    folder = output_folder(args)
    defaults = {
        "hourly_output": "hourly_traffic_summary",
        "peak_output": "peak_hour_summary",
        "junction_hourly_output": "junction_hourly_traffic_summary",
        "junction_peak_output": "junction_peak_hour_summary",
        "weekly_hourly_output": "weekly_hourly_traffic_summary",
        "weekly_peak_output": "weekly_peak_hour_summary",
        "junction_weekly_hourly_output": "junction_weekly_hourly_traffic_summary",
        "junction_weekly_peak_output": "junction_weekly_peak_hour_summary",
    }
    for attr, base_name in defaults.items():
        if getattr(args, attr) is None:
            setattr(args, attr, folder / dated_csv_name(base_name, suffix))
        else:
            setattr(args, attr, Path(getattr(args, attr)))


def print_peak_summary(rows, title, show_junction=False, limit=None):
    if not rows:
        print(f"No {title.lower()} rows found.")
        return

    print(f"\n{title}")
    print("-" * len(title))
    displayed_rows = rows if limit is None else rows[:limit]
    for row in displayed_rows:
        junction = ""
        if show_junction:
            label = row.get("junction_name") or row.get("source_row")
            junction = f" junction={label}"
        print(
            f"{row['model']} {row['date']}{junction}: {row['peak_hour']} "
            f"({row['peak_metric']}={row['peak_metric_value']}, "
            f"records={row['records']})"
        )
    if limit is not None and len(rows) > limit:
        print(f"... {len(rows) - limit} more rows written to CSV.")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create hourly traffic summaries and peak-hour reports."
    )
    parser.add_argument(
        "--inputs",
        nargs="+",
        default=None,
        help=(
            "Traffic observation CSVs to analyze. Defaults to "
            "traffic_observations_*.csv."
        ),
    )
    parser.add_argument(
        "--date",
        type=parse_date,
        help="Analyze one local requested date only, for example 2026-05-21.",
    )
    parser.add_argument(
        "--start-date",
        type=parse_date,
        help="Start local requested date for weekly/range analysis.",
    )
    parser.add_argument(
        "--end-date",
        type=parse_date,
        help="End local requested date for weekly/range analysis.",
    )
    parser.add_argument(
        "--peak-metric",
        default="congestion_index",
        choices=sorted(METRIC_TO_SUMMARY_FIELD),
        help=(
            "Metric used to choose peak hour. For traffic_speed_kmh, the lowest "
            "average speed is treated as peak traffic."
        ),
    )
    parser.add_argument(
        "--hourly-output",
        default=None,
        help="Output CSV for hourly averages. Defaults to a date-stamped filename.",
    )
    parser.add_argument(
        "--peak-output",
        default=None,
        help="Output CSV for peak hour by model/date. Defaults to a date-stamped filename.",
    )
    parser.add_argument(
        "--junction-hourly-output",
        default=None,
        help="Output CSV for hourly averages by junction. Defaults to a date-stamped filename.",
    )
    parser.add_argument(
        "--junction-peak-output",
        default=None,
        help="Output CSV for peak hour by junction/model/date. Defaults to a date-stamped filename.",
    )
    parser.add_argument(
        "--weekly-hourly-output",
        default=None,
        help="Output CSV for date-range hourly averages. Defaults to a date-stamped filename.",
    )
    parser.add_argument(
        "--weekly-peak-output",
        default=None,
        help="Output CSV for date-range peak hour by model. Defaults to a date-stamped filename.",
    )
    parser.add_argument(
        "--junction-weekly-hourly-output",
        default=None,
        help="Output CSV for date-range hourly averages by junction. Defaults to a date-stamped filename.",
    )
    parser.add_argument(
        "--junction-weekly-peak-output",
        default=None,
        help="Output CSV for date-range peak hour by junction/model. Defaults to a date-stamped filename.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.date and (args.start_date or args.end_date):
        print("Use either --date or --start-date/--end-date, not both.")
        return 2
    if args.start_date and args.end_date and args.start_date > args.end_date:
        print("--start-date must be before or equal to --end-date.")
        return 2
    resolve_output_paths(args)

    input_paths = args.inputs or discover_input_paths()
    if not input_paths:
        print("No input files found. Run traffic_collector.py first.")
        return 1

    rows = list(
        load_rows(
            input_paths,
            exact_date=args.date,
            start_date=args.start_date,
            end_date=args.end_date,
        )
    )
    if not rows:
        print("No matching OK traffic rows found for the selected inputs/date.")
        return 1

    hourly_rows = summarize_hourly(rows)
    peaks = peak_rows(hourly_rows, args.peak_metric)
    junction_hourly_rows = summarize_hourly(rows, by_junction=True)
    junction_peaks = peak_rows(
        junction_hourly_rows,
        args.peak_metric,
        by_junction=True,
    )

    write_csv(args.hourly_output, HOURLY_FIELDNAMES, hourly_rows)
    write_csv(args.peak_output, PEAK_FIELDNAMES, peaks)
    write_csv(
        args.junction_hourly_output,
        JUNCTION_HOURLY_FIELDNAMES,
        junction_hourly_rows,
    )
    write_csv(
        args.junction_peak_output,
        JUNCTION_PEAK_FIELDNAMES,
        junction_peaks,
    )

    print(f"Wrote {len(hourly_rows)} hourly rows to {args.hourly_output}")
    print(f"Wrote {len(peaks)} peak rows to {args.peak_output}")
    print(
        f"Wrote {len(junction_hourly_rows)} junction-hourly rows to "
        f"{args.junction_hourly_output}"
    )
    print(
        f"Wrote {len(junction_peaks)} junction peak rows to "
        f"{args.junction_peak_output}"
    )
    print_peak_summary(peaks, "Overall peak hour summary")
    print_peak_summary(
        junction_peaks,
        "Junction-wise peak hour summary",
        show_junction=True,
        limit=10,
    )

    if args.start_date or args.end_date:
        start_label = args.start_date.isoformat() if args.start_date else "first"
        end_label = args.end_date.isoformat() if args.end_date else "last"
        week_label = f"{start_label}_to_{end_label}"
        weekly_hourly_rows = summarize_hourly(rows, date_label=week_label)
        weekly_peaks = peak_rows(weekly_hourly_rows, args.peak_metric)
        junction_weekly_hourly_rows = summarize_hourly(
            rows,
            by_junction=True,
            date_label=week_label,
        )
        junction_weekly_peaks = peak_rows(
            junction_weekly_hourly_rows,
            args.peak_metric,
            by_junction=True,
        )

        write_csv(args.weekly_hourly_output, HOURLY_FIELDNAMES, weekly_hourly_rows)
        write_csv(args.weekly_peak_output, PEAK_FIELDNAMES, weekly_peaks)
        write_csv(
            args.junction_weekly_hourly_output,
            JUNCTION_HOURLY_FIELDNAMES,
            junction_weekly_hourly_rows,
        )
        write_csv(
            args.junction_weekly_peak_output,
            JUNCTION_PEAK_FIELDNAMES,
            junction_weekly_peaks,
        )

        print(
            f"\nWrote {len(weekly_hourly_rows)} weekly hourly rows to "
            f"{args.weekly_hourly_output}"
        )
        print(f"Wrote {len(weekly_peaks)} weekly peak rows to {args.weekly_peak_output}")
        print(
            f"Wrote {len(junction_weekly_hourly_rows)} junction-weekly-hourly "
            f"rows to {args.junction_weekly_hourly_output}"
        )
        print(
            f"Wrote {len(junction_weekly_peaks)} junction weekly peak rows to "
            f"{args.junction_weekly_peak_output}"
        )
        print_peak_summary(weekly_peaks, "Weekly peak hour summary")
        print_peak_summary(
            junction_weekly_peaks,
            "Junction-wise weekly peak hour summary",
            show_junction=True,
            limit=10,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
