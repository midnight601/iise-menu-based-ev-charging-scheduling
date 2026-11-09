from __future__ import annotations

import argparse
import csv
from dataclasses import replace
import json
import math
from pathlib import Path
from typing import Any

from menu_sp import (
    Request,
    Station,
    arrival_time,
    load_requests_xlsx,
    load_station_config,
    load_station_sheet_xlsx,
    solve_menu_sp,
)


def _normalise(value: object) -> str:
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def _time_slot(value: object, line_number: int) -> int:
    slot_value = float(value)
    if not math.isfinite(slot_value) or not math.isclose(
        slot_value, round(slot_value), abs_tol=1e-9
    ):
        raise ValueError(
            f"time_slot in CSV row {line_number} must be an integer time-slot index."
        )
    return int(round(slot_value))


def load_realized_requests_csv(
    path: str | Path,
) -> dict[int, list[dict[str, Any]]]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames:
            raise ValueError("The realized-request CSV must have a header row.")
        columns = {_normalise(name): name for name in reader.fieldnames}

        period_col = next(
            (columns[key] for key in ("timeslot", "period", "departureslot") if key in columns),
            None,
        )
        request_col = next(
            (columns[key] for key in ("requestid", "index", "id") if key in columns),
            None,
        )
        station_col = next(
            (columns[key] for key in ("stationid", "chosenstation", "actualstation") if key in columns),
            None,
        )
        if period_col is None or request_col is None:
            raise ValueError(
                "The realized-request CSV requires columns time_slot and request_id."
            )

        realized: dict[int, list[dict[str, Any]]] = {}
        seen: set[tuple[int, int]] = set()
        for line_number, row in enumerate(reader, start=2):
            raw_period = row.get(period_col)
            raw_request = row.get(request_col)
            if raw_period is None or raw_period.strip() == "":
                continue
            if raw_request is None or raw_request.strip() == "":
                raise ValueError(f"Missing request_id in CSV row {line_number}.")
            period = _time_slot(raw_period, line_number)
            request_id = int(float(raw_request))
            key = (period, request_id)
            if key in seen:
                raise ValueError(
                    f"Duplicate request {request_id} in time slot {period} (CSV row {line_number})."
                )
            seen.add(key)
            station_id = row.get(station_col, "").strip() if station_col else ""
            realized.setdefault(period, []).append(
                {"request_id": request_id, "station_id": station_id or None}
            )
    return realized


def advance_request_state(
    request: Request,
    next_period: int,
    segments_per_slot: float,
) -> Request:
    if not math.isfinite(segments_per_slot) or segments_per_slot < 0:
        raise ValueError("segments_per_slot must be finite and nonnegative.")
    new_position = min(
        request.max_segment,
        request.origin_segment + segments_per_slot,
    )
    return replace(
        request,
        departure_slot=float(next_period),
        origin_segment=float(new_position),
        probability=1.0,
    )


def update_charger_states(
    stations: list[Station],
    arrivals_by_station: dict[str, list[dict[str, Any]]],
    period_start: float,
    period_end: float,
) -> tuple[list[Station], list[dict[str, Any]]]:
    """Process EVs that reach stations within a slot and update charger states."""
    updated: list[Station] = []
    realized_waits: list[dict[str, Any]] = []

    for station in stations:
        available = list(sorted(station.initial_available))
        events = sorted(
            arrivals_by_station.get(station.station_id, []),
            key=lambda item: (
                item["arrival_time"],
                -item["probability"],
                item["request_id"],
            ),
        )
        for event in events:
            arrival = float(event["arrival_time"])
            if arrival < period_start - 1e-8 or arrival > period_end + 1e-8:
                raise ValueError(
                    f"Request {event['request_id']} has an arrival outside the current time slot."
                )
            charger_index = min(
                range(station.capacity),
                key=lambda index: (available[index], index),
            )
            service_start = max(available[charger_index], arrival)
            waiting_time = max(0.0, service_start - arrival)
            completion = service_start + float(event["duration"])
            available[charger_index] = completion
            available.sort()
            realized_waits.append(
                {
                    "request_id": int(event["request_id"]),
                    "station_id": station.station_id,
                    "arrival_time": arrival,
                    "waiting_time": waiting_time,
                    "service_start": service_start,
                    "service_completion": completion,
                    "is_carryover": bool(event["is_carryover"]),
                }
            )
        updated.append(
            Station(
                station_id=station.station_id,
                segment=station.segment,
                capacity=station.capacity,
                initial_available=tuple(available),
            )
        )

    realized_waits.sort(key=lambda item: (item["arrival_time"], item["request_id"]))
    return updated, realized_waits


def _period_index(requests: list[Request]) -> dict[int, list[Request]]:
    by_period: dict[int, list[Request]] = {}
    for request in requests:
        slot_value = request.departure_slot
        if not math.isfinite(slot_value) or not math.isclose(
            slot_value, round(slot_value), abs_tol=1e-9
        ):
            raise ValueError(
                f"Request {request.request_id} has a non-integer departure time slot."
            )
        by_period.setdefault(int(round(slot_value)), []).append(request)
    for period in by_period:
        by_period[period].sort(key=lambda item: item.request_id)
    return by_period


def run_rho(
    requests: list[Request],
    initial_stations: list[Station],
    travel_slots_per_segment: float,
    realized_by_period: dict[int, list[dict[str, Any]]] | None = None,
    time_limit: float | None = None,
    output_flag: int = 1,
    mip_gap: float = 0.0,
    seed: int = 1,
    threads: int | None = None,
    freeze_state: bool = False,
    segments_per_slot: float | None = None,
    max_periods: int = 10000,
) -> dict:
    if not requests:
        raise ValueError("The request workbook contains no potential requests.")
    if not initial_stations:
        raise ValueError("At least one charging station is required.")
    if not math.isfinite(travel_slots_per_segment) or travel_slots_per_segment <= 0:
        raise ValueError("travel_slots_per_segment must be positive and finite.")
    if max_periods <= 0:
        raise ValueError("max_periods must be positive.")

    if segments_per_slot is None:
        segments_per_slot = 1.0 / travel_slots_per_segment
    if not math.isfinite(segments_per_slot) or segments_per_slot < 0:
        raise ValueError("segments_per_slot must be finite and nonnegative.")

    new_requests_by_period = _period_index(requests)
    first_period = min(new_requests_by_period)
    last_new_period = max(new_requests_by_period)
    realized_by_period = realized_by_period

    all_request_ids = {request.request_id for request in requests}
    if realized_by_period is not None:
        for period, rows in realized_by_period.items():
            for row in rows:
                request_id = int(row["request_id"])
                if request_id not in all_request_ids:
                    raise ValueError(
                        f"Realized CSV contains request_id={request_id}, absent from the potential-request data."
                    )

    if not freeze_state and realized_by_period is None and last_new_period > first_period:
        raise ValueError(
            "Sequential RHO requires realized-request data to update station states. "
            "Provide --realized-csv. Use --freeze-state only for a diagnostic run."
        )

    current_stations = list(initial_stations)
    carryovers: dict[int, Request] = {}
    period_results: list[dict[str, Any]] = []
    period = first_period

    while period <= last_new_period or carryovers:
        if len(period_results) >= max_periods:
            raise RuntimeError(
                f"RHO exceeded max_periods={max_periods}; check the travel/state update rules."
            )

        new_requests = list(new_requests_by_period.get(period, []))
        carryover_requests = [carryovers[rid] for rid in sorted(carryovers)]
        period_requests = carryover_requests + new_requests
        period_requests.sort(key=lambda item: item.request_id)
        period_request_ids = {request.request_id for request in period_requests}
        if len(period_request_ids) != len(period_requests):
            raise ValueError(f"Duplicate request IDs in the Menu SP for time slot {period}.")

        result = solve_menu_sp(
            period_requests,
            current_stations,
            travel_slots_per_segment=travel_slots_per_segment,
            time_limit=time_limit,
            output_flag=output_flag,
            mip_gap=mip_gap,
            seed=seed,
            threads=threads,
        )
        if result.get("status_name") == "NO_INCUMBENT":
            raise RuntimeError(
                f"Menu SP at time slot {period} has no feasible incumbent. RHO cannot proceed."
            )

        result["period"] = period
        result["number_of_new_potential_requests"] = len(new_requests)
        result["number_of_carryover_requests_in"] = len(carryover_requests)
        result["number_of_potential_requests"] = len(period_requests)
        result["station_state_before"] = {
            station.station_id: list(station.initial_available)
            for station in current_stations
        }

        if freeze_state:
            result["new_realized_request_count"] = 0
            result["realized_request_count"] = 0
            result["realized_waits"] = []
            result["carryover_out"] = []
            result["station_state_after"] = result["station_state_before"]
            period_results.append(result)
            period += 1
            continue

        rows = (realized_by_period or {}).get(period, [])
        row_by_id = {int(row["request_id"]): row for row in rows}
        unexpected = set(row_by_id) - period_request_ids
        if unexpected:
            raise ValueError(
                f"Realized CSV has requests {sorted(unexpected)} in time slot {period}, "
                "but they are neither new potential requests nor active carryovers."
            )

        new_ids = {request.request_id for request in new_requests}
        carryover_ids = set(carryovers)
        realized_new_ids = set(row_by_id).intersection(new_ids)
        actual_requests = carryover_requests + [
            request for request in new_requests if request.request_id in realized_new_ids
        ]

        station_by_id = {station.station_id: station for station in current_stations}
        arrivals_by_station: dict[str, list[dict[str, Any]]] = {
            station.station_id: [] for station in current_stations
        }
        next_carryovers: dict[int, Request] = {}
        actual_station_choices: dict[str, str] = {}
        carryover_out: list[dict[str, Any]] = []
        period_end = float(period + 1)

        for request in actual_requests:
            request_id = request.request_id
            row = row_by_id.get(request_id, {})
            station_id = row.get("station_id") or result.get("assignment", {}).get(str(request_id))
            if station_id is None:
                raise RuntimeError(
                    f"No menu assignment or observed station choice for realized request {request_id} "
                    f"in time slot {period}."
                )
            station_id = str(station_id)
            if station_id not in station_by_id:
                raise ValueError(
                    f"Request {request_id} uses unknown station {station_id!r}."
                )
            station = station_by_id[station_id]
            arrival = arrival_time(request, station, travel_slots_per_segment)
            actual_station_choices[str(request_id)] = station_id

            if arrival <= period_end + 1e-8:
                arrivals_by_station[station_id].append(
                    {
                        "request_id": request_id,
                        "arrival_time": arrival,
                        "duration": request.duration,
                        "probability": request.probability,
                        "is_carryover": request_id in carryover_ids,
                    }
                )
            else:
                next_request = advance_request_state(
                    request,
                    next_period=period + 1,
                    segments_per_slot=segments_per_slot,
                )
                next_carryovers[request_id] = next_request
                carryover_out.append(
                    {
                        "request_id": request_id,
                        "station_recommended_this_slot": station_id,
                        "arrival_time_under_this_assignment": arrival,
                        "origin_segment_before_update": request.origin_segment,
                        "origin_segment_after_update": next_request.origin_segment,
                        "max_segment": next_request.max_segment,
                        "next_appearance_probability": next_request.probability,
                    }
                )

        current_stations, realized_waits = update_charger_states(
            current_stations,
            arrivals_by_station,
            period_start=float(period),
            period_end=period_end,
        )
        carryovers = next_carryovers

        result["new_realized_request_count"] = len(realized_new_ids)
        result["realized_request_count"] = len(actual_requests)
        result["actual_station_choices"] = actual_station_choices
        result["realized_waits"] = realized_waits
        result["carryover_out"] = carryover_out
        result["number_of_carryover_requests_out"] = len(carryovers)
        result["station_state_after"] = {
            station.station_id: list(station.initial_available)
            for station in current_stations
        }
        period_results.append(result)
        period += 1

    return {
        "method": (
            "RHO_frozen_state_diagnostic"
            if freeze_state else "RHO_sequential_single_period_Menu_SP"
        ),
        "number_of_periods": len(period_results),
        "travel_slots_per_segment": travel_slots_per_segment,
        "segments_per_slot": segments_per_slot,
        "period_results": period_results,
        "final_station_state": {
            station.station_id: list(station.initial_available)
            for station in current_stations
        },
        "remaining_carryovers": sorted(carryovers),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run RHO sequentially, solving one Menu SP for each time slot."
    )
    parser.add_argument("--input", required=True, help="Potential-request workbook (.xlsx or .xls).")
    parser.add_argument("--sheet", default="User", help="Worksheet containing potential requests.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--station-config", help="Station JSON configuration.")
    group.add_argument("--station-sheet", help="Station worksheet, e.g. CS.")
    parser.add_argument(
        "--station-workbook",
        default=None,
        help="Workbook containing the station worksheet. Defaults to --input.",
    )
    parser.add_argument("--travel-scale", type=float, default=None)
    parser.add_argument(
        "--segments-per-slot",
        type=float,
        default=None,
        help="EV location advance per slot in segment units; defaults to 1/travel-scale.",
    )
    parser.add_argument(
        "--realized-csv",
        default=None,
        help="CSV of realized requests: time_slot,request_id[,station_id].",
    )
    parser.add_argument(
        "--freeze-state",
        action="store_true",
        help="Diagnostic only: solve each slot without implementing arrivals or updating states.",
    )
    parser.add_argument("--max-periods", type=int, default=10000)
    parser.add_argument("--time-limit", type=float, default=None)
    parser.add_argument("--mip-gap", type=float, default=0.0)
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--output", default="rho_results.json")
    args = parser.parse_args()

    requests = load_requests_xlsx(args.input, args.sheet)
    if args.station_config:
        stations, config_travel_scale = load_station_config(args.station_config)
        travel_scale = args.travel_scale or config_travel_scale
    else:
        station_workbook = args.station_workbook or args.input
        stations = load_station_sheet_xlsx(station_workbook, args.station_sheet)
        travel_scale = args.travel_scale or 2.0

    realized = (
        load_realized_requests_csv(args.realized_csv)
        if args.realized_csv else None
    )
    results = run_rho(
        requests,
        stations,
        travel_slots_per_segment=travel_scale,
        realized_by_period=realized,
        time_limit=args.time_limit,
        output_flag=0 if args.quiet else 1,
        mip_gap=args.mip_gap,
        seed=args.seed,
        threads=args.threads,
        freeze_state=args.freeze_state,
        segments_per_slot=args.segments_per_slot,
        max_periods=args.max_periods,
    )
    results["input_workbook"] = str(args.input)
    results["input_sheet"] = args.sheet
    results["station_workbook"] = args.station_workbook or args.input
    results["realized_csv"] = args.realized_csv
    rendered = json.dumps(results, indent=2, ensure_ascii=False)
    Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
