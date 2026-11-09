from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Iterable


@dataclass(frozen=True)
class Request:
    request_id: int
    departure_slot: float
    duration: float
    origin_segment: float
    max_segment: float
    probability: float


@dataclass(frozen=True)
class Station:
    station_id: str
    segment: float
    capacity: int
    initial_available: tuple[float, ...]


@dataclass(frozen=True)
class Candidate:
    request_id: int
    arrival_time: float
    duration: float
    probability: float


class DataValidationError(ValueError):


def _normalise_header(value: object) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "")).lower()


def _read_worksheet(path: str | Path, sheet_name: str) -> list[tuple]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    suffix = path.suffix.lower()
    if suffix == ".xls":
        try:
            import xlrd
        except ImportError as exc:
            raise RuntimeError(
                "Reading .xls files requires xlrd. Install it with 'pip install xlrd>=2.0.1'."
            ) from exc
        workbook = xlrd.open_workbook(str(path), on_demand=True)
        try:
            try:
                worksheet = workbook.sheet_by_name(sheet_name)
            except xlrd.biffh.XLRDError as exc:
                raise DataValidationError(
                    f"Worksheet {sheet_name!r} not found in {path.name}."
                ) from exc
            rows = []
            for row_index in range(worksheet.nrows):
                values = []
                for column_index in range(worksheet.ncols):
                    cell = worksheet.cell(row_index, column_index)
                    values.append(None if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK) else cell.value)
                rows.append(tuple(values))
            return rows
        finally:
            workbook.release_resources()

    if suffix not in {".xlsx", ".xlsm"}:
        raise DataValidationError(
            f"Unsupported workbook format {suffix!r}. Use .xlsx, .xlsm, or .xls."
        )
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("Reading .xlsx files requires openpyxl.") from exc

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet_name not in workbook.sheetnames:
            raise DataValidationError(
                f"Worksheet {sheet_name!r} not found. Available sheets: {workbook.sheetnames}"
            )
        return list(workbook[sheet_name].iter_rows(values_only=True))
    finally:
        workbook.close()


def load_requests_xlsx(path: str | Path, sheet_name: str) -> list[Request]:
    rows = _read_worksheet(path, sheet_name)
    if not rows:
        raise DataValidationError(f"Worksheet {sheet_name!r} is empty.")

    header = rows[0]
    columns = {
        _normalise_header(value): index
        for index, value in enumerate(header)
        if value is not None
    }

    def locate(*aliases: str, required: bool = True) -> int | None:
        for alias in aliases:
            key = _normalise_header(alias)
            if key in columns:
                return columns[key]
        if required:
            raise DataValidationError(
                f"Missing column in {sheet_name!r}. Expected one of {aliases}."
            )
        return None

    id_col = locate("index", "request_id", "id", "编号", required=False)
    departure_col = locate("发出时段", "time slot", "departure slot", "departure_slot")
    duration_col = locate("充电时长", "charging duration", "duration")
    origin_col = locate("发出路段", "location segment", "origin segment", "origin_segment")
    max_col = locate(
        "最大覆盖里程", "maximum reachable segment", "max segment", "max_segment",
        required=False,
    )
    range_col = locate(
        "剩余电量（路段数）", "remaining range", "soc range", "state of charge",
        required=False,
    )
    probability_col = locate(
        "生成概率", "appearance probability", "probability", required=False
    )

    requests: list[Request] = []
    for row_number, row in enumerate(rows[1:], start=2):
        if not row or all(value is None for value in row):
            continue

        def value_at(column: int | None, default: object = None) -> object:
            if column is None or column >= len(row) or row[column] is None:
                return default
            return row[column]

        raw_id = value_at(id_col, len(requests))
        raw_departure = value_at(departure_col)
        raw_duration = value_at(duration_col)
        raw_origin = value_at(origin_col)
        raw_max = value_at(max_col)
        raw_range = value_at(range_col)
        raw_probability = value_at(probability_col, 1.0)

        if raw_departure is None or raw_duration is None or raw_origin is None:
            raise DataValidationError(
                f"Missing request data at {sheet_name}!row {row_number}."
            )
        origin = float(raw_origin)
        if raw_max is None:
            if raw_range is None:
                raise DataValidationError(
                    f"Cannot determine max_segment at {sheet_name}!row {row_number}."
                )
            max_segment = origin + float(raw_range)
        else:
            max_segment = float(raw_max)

        request = Request(
            request_id=int(float(raw_id)),
            departure_slot=float(raw_departure),
            duration=float(raw_duration),
            origin_segment=origin,
            max_segment=max_segment,
            probability=float(raw_probability),
        )
        numeric_values = (
            request.departure_slot,
            request.duration,
            request.origin_segment,
            request.max_segment,
            request.probability,
        )
        if not all(math.isfinite(value) for value in numeric_values):
            raise DataValidationError(
                f"Request {request.request_id} contains a non-finite numeric value."
            )
        if request.duration < 0:
            raise DataValidationError(
                f"Request {request.request_id} has negative charging duration."
            )
        if request.max_segment < request.origin_segment:
            raise DataValidationError(
                f"Request {request.request_id} has max_segment < origin_segment."
            )
        if not 0.0 <= request.probability <= 1.0:
            raise DataValidationError(
                f"Request {request.request_id} has probability outside [0, 1]."
            )
        requests.append(request)

    ids = [request.request_id for request in requests]
    if len(ids) != len(set(ids)):
        raise DataValidationError(
            f"Request IDs are not unique in worksheet {sheet_name!r}."
        )
    return requests


def load_station_config(path: str | Path) -> tuple[list[Station], float]:
    path = Path(path)
    with path.open("r", encoding="utf-8") as file:
        raw = json.load(file)

    travel_scale = float(raw.get("travel_slots_per_segment", 2.0))
    if not math.isfinite(travel_scale) or travel_scale <= 0:
        raise DataValidationError("travel_slots_per_segment must be positive and finite.")

    stations: list[Station] = []
    for index, item in enumerate(raw.get("stations", [])):
        capacity = int(item["capacity"])
        segment = float(item["segment"])
        if capacity <= 0:
            raise DataValidationError(f"Station {index} has nonpositive capacity.")
        if not math.isfinite(segment):
            raise DataValidationError(f"Station {index} has a non-finite segment. ")
        initial = tuple(
            sorted(float(value) for value in item.get("initial_available", [0.0] * capacity))
        )
        if len(initial) != capacity or not all(math.isfinite(value) for value in initial):
            raise DataValidationError(
                f"Station {index}: initial_available must contain {capacity} finite values."
            )
        stations.append(
            Station(
                station_id=str(item.get("id", f"Station{index + 1}")),
                segment=segment,
                capacity=capacity,
                initial_available=initial,
            )
        )
    if not stations:
        raise DataValidationError("The station configuration contains no stations.")
    station_ids = [station.station_id for station in stations]
    if len(station_ids) != len(set(station_ids)):
        raise DataValidationError("Station IDs must be unique.")
    return stations, travel_scale


def load_station_sheet_xlsx(
    workbook_path: str | Path,
    sheet_name: str,
) -> list[Station]:
    rows = _read_worksheet(workbook_path, sheet_name)
    if len(rows) < 2:
        raise DataValidationError(f"No station records found in worksheet {sheet_name!r}.")

    stations: list[Station] = []
    for row_index, row in enumerate(rows[1:], start=2):
        if len(row) < 3 or row[1] is None or row[2] is None:
            continue
        capacity = int(float(row[2]))
        if capacity <= 0:
            raise DataValidationError(f"Station row {row_index} has invalid capacity.")
        raw_state = list(row[4 : 4 + capacity])
        if len(raw_state) < capacity:
            raw_state.extend([0.0] * (capacity - len(raw_state)))
        initial = tuple(
            sorted(0.0 if value is None else float(value) for value in raw_state)
        )
        if not all(math.isfinite(value) for value in initial):
            raise DataValidationError(f"Station row {row_index} has a non-finite state.")
        raw_id = row[0]
        if raw_id is None:
            station_id = f"Station{len(stations) + 1}"
        else:
            try:
                numeric_id = float(raw_id)
                station_id = f"Station{int(numeric_id) + 1}" if numeric_id.is_integer() else str(raw_id)
            except (TypeError, ValueError):
                station_id = str(raw_id)
        stations.append(
            Station(
                station_id=station_id,
                segment=float(row[1]),
                capacity=capacity,
                initial_available=initial,
            )
        )
    if not stations:
        raise DataValidationError(f"No station records found in worksheet {sheet_name!r}.")
    station_ids = [station.station_id for station in stations]
    if len(station_ids) != len(set(station_ids)):
        raise DataValidationError("Station IDs must be unique in the station worksheet.")
    return stations


def arrival_time(
    request: Request, station: Station, travel_slots_per_segment: float
) -> float:
    if not math.isfinite(travel_slots_per_segment) or travel_slots_per_segment <= 0:
        raise DataValidationError("travel_slots_per_segment must be positive and finite.")
    if not request.origin_segment - 1e-9 <= station.segment <= request.max_segment + 1e-9:
        raise DataValidationError(
            f"Station {station.station_id} is not accessible to request {request.request_id}."
        )
    return request.departure_slot + (
        station.segment - request.origin_segment
    ) * travel_slots_per_segment


def make_candidate(
    request: Request, station: Station, travel_slots_per_segment: float
) -> Candidate:
    return Candidate(
        request_id=request.request_id,
        arrival_time=arrival_time(request, station, travel_slots_per_segment),
        duration=request.duration,
        probability=request.probability,
    )


def fcfs_sort(candidates: Iterable[Candidate]) -> list[Candidate]:
    """FCFS order with the probability-descending tie-break in the original code."""
    return sorted(
        candidates,
        key=lambda item: (item.arrival_time, -item.probability, item.request_id),
    )


def _shared_library_path() -> Path:
    base = Path(__file__).resolve().parent
    if sys.platform == "darwin":
        return base / "libwaiting_time.dylib"
    if os.name == "nt":
        return base / "waiting_time.dll"
    return base / "libwaiting_time.so"


def load_waiting_time_library(path: str | Path | None = None) -> ctypes.CDLL:
    source = Path(__file__).resolve().parent / "waiting_time.c"
    if not source.exists():
        raise FileNotFoundError(f"Cannot find C source file: {source}")

    library_path = Path(path) if path is not None else _shared_library_path()
    if path is None and (
        not library_path.exists() or library_path.stat().st_mtime < source.stat().st_mtime
    ):
        compiler = (
            os.environ.get("CC")
            or shutil.which("cc")
            or shutil.which("gcc")
            or shutil.which("clang")
        )
        if compiler is None:
            raise RuntimeError(
                "A C compiler (cc, gcc, or clang) is required to build waiting_time.c."
            )
        if sys.platform == "darwin":
            command = [
                compiler, "-O3", "-std=c11", "-dynamiclib", "-fPIC",
                str(source), "-o", str(library_path), "-lm",
            ]
        elif os.name == "nt":
            command = [
                compiler, "-O3", "-std=c11", "-shared", str(source),
                "-o", str(library_path), "-lm",
            ]
        else:
            command = [
                compiler, "-O3", "-std=c11", "-fPIC", "-shared",
                str(source), "-o", str(library_path), "-lm",
            ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                "Compilation of waiting_time.c failed:\n" + (exc.stderr or exc.stdout or str(exc))
            ) from exc

    if not library_path.exists():
        raise FileNotFoundError(f"Shared library not found: {library_path}")

    library = ctypes.CDLL(str(library_path))
    double_pointer = ctypes.POINTER(ctypes.c_double)
    library.expected_conditional_waits.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        double_pointer,
        double_pointer,
        double_pointer,
        double_pointer,
        double_pointer,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    library.expected_conditional_waits.restype = ctypes.c_int
    return library


def expected_conditional_waits_c(
    candidates: Iterable[Candidate],
    capacity: int,
    initial_available: Iterable[float],
    library: ctypes.CDLL | None = None,
) -> tuple[dict[int, float], dict[str, int]]:
    ordered = fcfs_sort(candidates)
    initial = tuple(sorted(float(value) for value in initial_available))
    if capacity <= 0 or len(initial) != capacity:
        raise ValueError("capacity must be positive and match initial_available length.")
    if not ordered:
        return {}, {"processed_requests": 0, "max_states": 1}
    if len({item.request_id for item in ordered}) != len(ordered):
        raise ValueError("Request IDs must be unique within a station subset.")

    n = len(ordered)
    probabilities = (ctypes.c_double * n)(*(item.probability for item in ordered))
    durations = (ctypes.c_double * n)(*(item.duration for item in ordered))
    arrivals = (ctypes.c_double * n)(*(item.arrival_time for item in ordered))
    initial_array = (ctypes.c_double * capacity)(*initial)
    waits = (ctypes.c_double * n)()
    max_states = ctypes.c_size_t(0)

    lib = library or load_waiting_time_library()
    status = lib.expected_conditional_waits(
        n,
        capacity,
        probabilities,
        durations,
        arrivals,
        initial_array,
        waits,
        ctypes.byref(max_states),
    )
    if status != 0:
        messages = {
            1: "invalid dimensions or null pointer",
            2: "invalid numeric input",
            3: "memory allocation failure",
            4: "size arithmetic overflow",
            5: "internal state-capacity error",
        }
        raise RuntimeError(
            f"C waiting-time evaluator failed ({status}): {messages.get(status, 'unknown error')}"
        )

    values = {item.request_id: float(waits[i]) for i, item in enumerate(ordered)}
    return values, {
        "processed_requests": n,
        "max_states": int(max_states.value),
    }


def solve_menu_sp(
    requests: list[Request],
    stations: list[Station],
    travel_slots_per_segment: float,
    time_limit: float | None = None,
    output_flag: int = 1,
    mip_gap: float = 0.0,
    seed: int = 1,
    threads: int | None = None,
    c_library: ctypes.CDLL | None = None,
) -> dict:
    """Solve one-period Menu SP exactly by lazy-constraint Branch-and-Cut.
    """
    if not stations:
        raise ValueError("At least one charging station is required.")
    if not math.isfinite(travel_slots_per_segment) or travel_slots_per_segment <= 0:
        raise ValueError("travel_slots_per_segment must be positive and finite.")
    if len({request.request_id for request in requests}) != len(requests):
        raise ValueError("Request IDs must be unique within a Menu SP period.")
    if not requests:
        return {
            "status": None,
            "status_name": "EMPTY_PERIOD",
            "objective": 0.0,
            "runtime_seconds": 0.0,
            "assignment": {},
            "station_expected_wait": {station.station_id: 0.0 for station in stations},
            "conditional_wait_by_request": {},
            "cuts_added": 0,
            "unique_station_subsets_evaluated": 0,
            "max_c_state_count": 1,
        }

    try:
        import gurobipy as gp
        from gurobipy import GRB
    except ImportError as exc:
        raise RuntimeError(
            "gurobipy is required. Install Gurobi's Python package and activate a valid license."
        ) from exc

    library = c_library or load_waiting_time_library()
    eligible: dict[int, list[int]] = {}
    candidates_by_pair: dict[tuple[int, int], Candidate] = {}
    request_ids_by_station: list[list[int]] = [[] for _ in stations]

    for request in requests:
        eligible_stations: list[int] = []
        for station_index, station in enumerate(stations):
            if request.origin_segment - 1e-9 <= station.segment <= request.max_segment + 1e-9:
                candidate = make_candidate(request, station, travel_slots_per_segment)
                candidates_by_pair[(request.request_id, station_index)] = candidate
                eligible_stations.append(station_index)
                request_ids_by_station[station_index].append(request.request_id)
        if not eligible_stations:
            raise DataValidationError(
                f"Request {request.request_id} cannot reach any configured charging station."
            )
        eligible[request.request_id] = eligible_stations

    feasible_pairs = [
        (request.request_id, station_index)
        for request in requests
        for station_index in eligible[request.request_id]
    ]

    model = gp.Model("Single_Period_Menu_SP")
    y = model.addVars(feasible_pairs, vtype=GRB.BINARY, name="y")
    eta = model.addVars(
        range(len(stations)), lb=0.0, vtype=GRB.CONTINUOUS, name="eta"
    )

    for request in requests:
        rid = request.request_id
        model.addConstr(
            gp.quicksum(y[rid, station_index] for station_index in eligible[rid]) == 1,
            name=f"assign_{rid}",
        )

    model.setObjective(gp.quicksum(eta[m] for m in range(len(stations))), GRB.MINIMIZE)
    model.Params.LazyConstraints = 1
    model.Params.MIPGap = float(mip_gap)
    model.Params.OutputFlag = int(output_flag)
    model.Params.Seed = int(seed)
    if threads is not None:
        model.Params.Threads = int(threads)
    if time_limit is not None:
        model.Params.TimeLimit = float(time_limit)

    model._y = y
    model._eta = eta
    model._request_ids_by_station = request_ids_by_station
    model._candidate_by_pair = candidates_by_pair
    model._stations = stations
    model._c_library = library
    model._seen_cuts = set()
    model._cost_cache = {}
    model._callback_error = None
    model._cuts_added = 0
    model._max_c_state_count = 1
    model._callback_lock = threading.Lock()

    def lazy_callback(cb_model, where):
        if where != GRB.Callback.MIPSOL:
            return
        try:
            with cb_model._callback_lock:
                incumbent = cb_model.cbGetSolution(cb_model._y)
                for station_index, station_request_ids in enumerate(
                    cb_model._request_ids_by_station
                ):
                    assigned = tuple(
                        sorted(
                            rid for rid in station_request_ids
                            if incumbent[rid, station_index] > 0.5
                        )
                    )
                    if not assigned:
                        continue

                    cut_key = (station_index, assigned)
                    if cut_key in cb_model._seen_cuts:
                        continue

                    cew = cb_model._cost_cache.get(cut_key)
                    if cew is None:
                        station_candidates = [
                            cb_model._candidate_by_pair[(rid, station_index)]
                            for rid in assigned
                        ]
                        waits, stats = expected_conditional_waits_c(
                            station_candidates,
                            capacity=cb_model._stations[station_index].capacity,
                            initial_available=cb_model._stations[station_index].initial_available,
                            library=cb_model._c_library,
                        )
                        cew = math.fsum(waits.values())
                        cb_model._cost_cache[cut_key] = cew
                        cb_model._max_c_state_count = max(
                            cb_model._max_c_state_count, stats["max_states"]
                        )

                    if cew > 1e-12:
                        lhs = gp.quicksum(
                            cb_model._y[rid, station_index] for rid in assigned
                        ) - (len(assigned) - 1)
                        cb_model.cbLazy(cb_model._eta[station_index] >= cew * lhs)
                        cb_model._cuts_added += 1
                    cb_model._seen_cuts.add(cut_key)
        except Exception as exc:
            cb_model._callback_error = exc
            cb_model.terminate()

    start = time.perf_counter()
    try:
        model.optimize(lazy_callback)
        runtime = time.perf_counter() - start
        if model._callback_error is not None:
            raise RuntimeError(
                "An error occurred inside the Gurobi callback."
            ) from model._callback_error

        if model.SolCount == 0:
            return {
                "status": int(model.Status),
                "status_name": "NO_INCUMBENT",
                "objective": None,
                "runtime_seconds": runtime,
                "assignment": {},
                "station_expected_wait": {},
                "conditional_wait_by_request": {},
                "cuts_added": int(model._cuts_added),
                "unique_station_subsets_evaluated": len(model._seen_cuts),
                "max_c_state_count": int(model._max_c_state_count),
            }

        assignment: dict[str, str] = {}
        assigned_by_station: list[list[int]] = [[] for _ in stations]
        for request in requests:
            rid = request.request_id
            selected = [m for m in eligible[rid] if y[rid, m].X > 0.5]
            if len(selected) != 1:
                raise RuntimeError(
                    f"Gurobi returned an invalid assignment for request {rid}."
                )
            station_index = selected[0]
            assignment[str(rid)] = stations[station_index].station_id
            assigned_by_station[station_index].append(rid)

        station_expected_wait: dict[str, float] = {}
        conditional_wait_by_request: dict[str, float] = {}
        for station_index, station in enumerate(stations):
            ids = assigned_by_station[station_index]
            if not ids:
                station_expected_wait[station.station_id] = 0.0
                continue
            station_candidates = [
                candidates_by_pair[(rid, station_index)] for rid in ids
            ]
            waits, stats = expected_conditional_waits_c(
                station_candidates,
                capacity=station.capacity,
                initial_available=station.initial_available,
                library=library,
            )
            station_expected_wait[station.station_id] = math.fsum(waits.values())
            conditional_wait_by_request.update(
                {str(rid): value for rid, value in waits.items()}
            )
            model._max_c_state_count = max(
                model._max_c_state_count, stats["max_states"]
            )

        recomputed_objective = math.fsum(station_expected_wait.values())
        status_name = (
            "OPTIMAL" if model.Status == GRB.OPTIMAL
            else "FEASIBLE_NOT_PROVEN_OPTIMAL"
        )
        return {
            "status": int(model.Status),
            "status_name": status_name,
            "objective": float(model.ObjVal),
            "recomputed_objective": recomputed_objective,
            "objective_consistency_error": abs(float(model.ObjVal) - recomputed_objective),
            "runtime_seconds": runtime,
            "mip_gap": float(model.MIPGap),
            "assignment": assignment,
            "station_expected_wait": station_expected_wait,
            "conditional_wait_by_request": conditional_wait_by_request,
            "cuts_added": int(model._cuts_added),
            "unique_station_subsets_evaluated": len(model._seen_cuts),
            "max_c_state_count": int(model._max_c_state_count),
        }
    finally:
        model.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Solve one single-period Menu SP [ReSP] by Branch-and-Cut."
    )
    parser.add_argument("--input", required=True, help="Request workbook (.xlsx or .xls).")
    parser.add_argument("--sheet", default="User", help="Worksheet containing requests.")
    parser.add_argument("--period", type=float, default=None, help="Optional departure slot to filter.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--station-config", help="Station JSON configuration.")
    group.add_argument("--station-sheet", help="Station worksheet, e.g. CS.")
    parser.add_argument(
        "--station-workbook",
        default=None,
        help="Workbook containing the station worksheet. Defaults to --input.",
    )
    parser.add_argument("--travel-scale", type=float, default=2.0)
    parser.add_argument("--time-limit", type=float, default=None)
    parser.add_argument("--mip-gap", type=float, default=0.0)
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    requests = load_requests_xlsx(args.input, args.sheet)
    if args.period is not None:
        requests = [
            request for request in requests
            if math.isclose(request.departure_slot, args.period, abs_tol=1e-9)
        ]
    if args.station_config:
        stations, travel_scale = load_station_config(args.station_config)
    else:
        station_workbook = args.station_workbook or args.input
        stations = load_station_sheet_xlsx(station_workbook, args.station_sheet)
        travel_scale = args.travel_scale

    result = solve_menu_sp(
        requests,
        stations,
        travel_slots_per_segment=travel_scale,
        time_limit=args.time_limit,
        output_flag=0 if args.quiet else 1,
        mip_gap=args.mip_gap,
        seed=args.seed,
        threads=args.threads,
    )
    result["period"] = args.period
    result["input_workbook"] = str(args.input)
    result["input_sheet"] = args.sheet
    result["number_of_potential_requests"] = len(requests)
    result["travel_slots_per_segment"] = travel_scale
    rendered = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
