
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import time
from typing import Iterable

try:
    import numpy as np
except ImportError:
    np = None

from menu_sp import (
    Candidate,
    Request,
    Station,
    load_requests_xlsx,
    load_station_config,
    load_station_sheet_xlsx,
    make_candidate,
)

try:
    if np is None:
        raise ImportError("NumPy is unavailable")
    from numba import njit
except ImportError:
    njit = None


@dataclass(frozen=True)
class PricingResult:
    sequence: tuple[int, ...]
    waiting_cost: float
    reduced_cost: float
    labels_processed: int
    labels_created: int
    max_labels_at_vertex: int


def _sort_full_information(candidates: Iterable[Candidate]) -> list[Candidate]:
    """FCFS order for realized EVs; ties are resolved by request ID."""
    return sorted(candidates, key=lambda item: (item.arrival_time, item.request_id))


def deterministic_waiting_cost(
    candidates: Iterable[Candidate],
    capacity: int,
    initial_available: Iterable[float],
) -> float:
    if capacity <= 0:
        raise ValueError("capacity must be positive.")
    available = sorted(float(value) for value in initial_available)
    if len(available) != capacity:
        raise ValueError("initial_available length must equal station capacity.")
    total_wait = 0.0
    for candidate in _sort_full_information(candidates):
        start = max(available[0], candidate.arrival_time)
        total_wait += max(0.0, start - candidate.arrival_time)
        available[0] = start + candidate.duration
        available.sort()
    return total_wait


def _state_no_later(
    state_a: tuple[float, ...], state_b: tuple[float, ...], tolerance: float
) -> bool:
    return all(a <= b + tolerance for a, b in zip(state_a, state_b))


def _dominates(a: tuple, b: tuple, tolerance: float) -> bool:
    """A label dominates another if it has no larger cost and no later servers."""
    cost_a, state_a = a[0], a[1]
    cost_b, state_b = b[0], b[1]
    return cost_a <= cost_b + tolerance and _state_no_later(state_a, state_b, tolerance)


def price_station_python(
    candidates: list[Candidate],
    capacity: int,
    initial_available: Iterable[float],
    station_dual: float,
    customer_duals: dict[int, float],
    tolerance: float = 1e-9,
) -> PricingResult:
    """Exact Python label-setting pricing, including the empty sequence."""
    if capacity <= 0:
        raise ValueError("capacity must be positive.")
    initial = tuple(sorted(float(value) for value in initial_available))
    if len(initial) != capacity:
        raise ValueError("initial_available length must equal station capacity.")
    ordered = _sort_full_information(candidates)
    if len({item.request_id for item in ordered}) != len(ordered):
        raise ValueError("Request IDs must be unique within each station pricing problem.")
    for item in ordered:
        if item.request_id not in customer_duals:
            raise KeyError(f"Missing customer dual for request {item.request_id}.")


    best_sequence: tuple[int, ...] = tuple()
    best_waiting_cost = 0.0
    best_reduced_cost = -float(station_dual)
    if not ordered:
        return PricingResult(tuple(), 0.0, best_reduced_cost, 0, 0, 0)

    labels_at_a: list[list[tuple[float, tuple[float, ...], float, tuple[int, ...]]]] = [
        [] for _ in ordered
    ]
    for i, request in enumerate(ordered):
        first_wait = max(0.0, initial[0] - request.arrival_time)
        labels_at_a[i].append((first_wait, initial, first_wait, tuple()))

    labels_processed = 0
    labels_created = len(ordered)
    max_frontier = 1

    for i, current in enumerate(ordered):
        for prefix_cost, state, prefix_wait, prefix_ids in list(labels_at_a[i]):
            labels_processed += 1
            service_start = max(state[0], current.arrival_time)
            completion = service_start + current.duration
            updated_state = tuple(sorted((*state[1:], completion)))
            updated_cost = prefix_cost - float(customer_duals[current.request_id])
            updated_wait = prefix_wait
            sequence = prefix_ids + (current.request_id,)
            reduced_cost = updated_cost - float(station_dual)

            if reduced_cost < best_reduced_cost - tolerance:
                best_sequence = sequence
                best_waiting_cost = updated_wait
                best_reduced_cost = reduced_cost

            for j in range(i + 1, len(ordered)):
                next_request = ordered[j]
                next_wait = max(0.0, updated_state[0] - next_request.arrival_time)
                candidate = (
                    updated_cost + next_wait,
                    updated_state,
                    updated_wait + next_wait,
                    sequence,
                )
                frontier = labels_at_a[j]

                if any(_dominates(existing, candidate, tolerance) for existing in frontier):
                    continue
                frontier[:] = [
                    existing
                    for existing in frontier
                    if not _dominates(candidate, existing, tolerance)
                ]
                frontier.append(candidate)
                labels_created += 1
                max_frontier = max(max_frontier, len(frontier))

    return PricingResult(
        sequence=best_sequence,
        waiting_cost=best_waiting_cost,
        reduced_cost=best_reduced_cost,
        labels_processed=labels_processed,
        labels_created=labels_created,
        max_labels_at_vertex=max_frontier,
    )


if njit is not None:
    @njit(cache=True)
    def _price_core_numba(arrivals, durations, duals, initial, station_dual, tolerance):
        n = len(arrivals)
        capacity = len(initial)
        if n == 0:
            return np.empty(0, dtype=np.int64), 0.0, 0.0, 0, 0, 0

        label_capacity = max(1024, n * 16)
        label_states = np.empty((label_capacity, capacity), dtype=np.float64)
        label_costs = np.empty(label_capacity, dtype=np.float64)
        label_waits = np.empty(label_capacity, dtype=np.float64)
        label_paths = np.empty(label_capacity, dtype=np.int64)
        label_next = np.empty(label_capacity, dtype=np.int64)
        heads = np.full(n, -1, dtype=np.int64)
        frontier_counts = np.zeros(n, dtype=np.int64)

        path_capacity = label_capacity
        path_parent = np.empty(path_capacity, dtype=np.int64)
        path_request = np.empty(path_capacity, dtype=np.int64)
        path_parent[0] = -1
        path_request[0] = -1
        path_count = 1

        # O -> A_i labels: each possible first request starts from the same state.
        label_count = n
        for i in range(n):
            wait = initial[0] - arrivals[i]
            if wait < 0.0:
                wait = 0.0
            for k in range(capacity):
                label_states[i, k] = initial[k]
            label_costs[i] = wait
            label_waits[i] = wait
            label_paths[i] = 0
            label_next[i] = -1
            heads[i] = i
            frontier_counts[i] = 1

        best_reduced_cost = 1.0e300
        best_waiting_cost = 0.0
        best_path_id = -1
        labels_processed = 0
        labels_created = n
        max_frontier = 1

        for i in range(n):
            current_label = heads[i]
            while current_label != -1:
                next_current_label = label_next[current_label]
                labels_processed += 1

                # Include request i and update the earliest available charger.
                service_start = label_states[current_label, 0]
                if arrivals[i] > service_start:
                    service_start = arrivals[i]
                completion = service_start + durations[i]

                updated_state = np.empty(capacity, dtype=np.float64)
                for k in range(capacity - 1):
                    updated_state[k] = label_states[current_label, k + 1]
                updated_state[capacity - 1] = completion
                for k in range(1, capacity):
                    value = updated_state[k]
                    pos = k - 1
                    while pos >= 0 and updated_state[pos] > value:
                        updated_state[pos + 1] = updated_state[pos]
                        pos -= 1
                    updated_state[pos + 1] = value

                updated_cost = label_costs[current_label] - duals[i]
                updated_wait = label_waits[current_label]

                if path_count >= path_capacity:
                    new_capacity = path_capacity * 2
                    new_parent = np.empty(new_capacity, dtype=np.int64)
                    new_request = np.empty(new_capacity, dtype=np.int64)
                    for k in range(path_count):
                        new_parent[k] = path_parent[k]
                        new_request[k] = path_request[k]
                    path_parent = new_parent
                    path_request = new_request
                    path_capacity = new_capacity

                new_path_id = path_count
                path_parent[new_path_id] = label_paths[current_label]
                path_request[new_path_id] = i
                path_count += 1

                reduced_cost = updated_cost - station_dual
                if reduced_cost < best_reduced_cost - tolerance:
                    best_reduced_cost = reduced_cost
                    best_waiting_cost = updated_wait
                    best_path_id = new_path_id

                # Extend B_i -> A_j and retain only nondominated labels.
                for j in range(i + 1, n):
                    next_wait = updated_state[0] - arrivals[j]
                    if next_wait < 0.0:
                        next_wait = 0.0
                    candidate_cost = updated_cost + next_wait
                    candidate_wait = updated_wait + next_wait

                    dominated = False
                    existing = heads[j]
                    while existing != -1:
                        if label_costs[existing] <= candidate_cost + tolerance:
                            state_no_later = True
                            for k in range(capacity):
                                if label_states[existing, k] > updated_state[k] + tolerance:
                                    state_no_later = False
                                    break
                            if state_no_later:
                                dominated = True
                                break
                        existing = label_next[existing]
                    if dominated:
                        continue

                    previous = -1
                    existing = heads[j]
                    while existing != -1:
                        next_existing = label_next[existing]
                        candidate_dominates = candidate_cost <= label_costs[existing] + tolerance
                        if candidate_dominates:
                            for k in range(capacity):
                                if updated_state[k] > label_states[existing, k] + tolerance:
                                    candidate_dominates = False
                                    break
                        if candidate_dominates:
                            if previous == -1:
                                heads[j] = next_existing
                            else:
                                label_next[previous] = next_existing
                            frontier_counts[j] -= 1
                        else:
                            previous = existing
                        existing = next_existing

                    if label_count >= label_capacity:
                        new_capacity = label_capacity * 2
                        new_states = np.empty((new_capacity, capacity), dtype=np.float64)
                        new_costs = np.empty(new_capacity, dtype=np.float64)
                        new_waits = np.empty(new_capacity, dtype=np.float64)
                        new_paths = np.empty(new_capacity, dtype=np.int64)
                        new_next = np.empty(new_capacity, dtype=np.int64)
                        for k in range(label_count):
                            new_costs[k] = label_costs[k]
                            new_waits[k] = label_waits[k]
                            new_paths[k] = label_paths[k]
                            new_next[k] = label_next[k]
                            for q in range(capacity):
                                new_states[k, q] = label_states[k, q]
                        label_states = new_states
                        label_costs = new_costs
                        label_waits = new_waits
                        label_paths = new_paths
                        label_next = new_next
                        label_capacity = new_capacity

                    new_label_id = label_count
                    for k in range(capacity):
                        label_states[new_label_id, k] = updated_state[k]
                    label_costs[new_label_id] = candidate_cost
                    label_waits[new_label_id] = candidate_wait
                    label_paths[new_label_id] = new_path_id
                    label_next[new_label_id] = heads[j]
                    heads[j] = new_label_id
                    label_count += 1
                    labels_created += 1
                    frontier_counts[j] += 1
                    if frontier_counts[j] > max_frontier:
                        max_frontier = frontier_counts[j]

                current_label = next_current_label

        if best_path_id < 0:
            return np.empty(0, dtype=np.int64), 0.0, 0.0, labels_processed, labels_created, max_frontier

        sequence_length = 0
        path_id = best_path_id
        while path_id > 0:
            sequence_length += 1
            path_id = path_parent[path_id]
        sequence = np.empty(sequence_length, dtype=np.int64)
        path_id = best_path_id
        position = sequence_length - 1
        while path_id > 0:
            sequence[position] = path_request[path_id]
            position -= 1
            path_id = path_parent[path_id]

        return (
            sequence,
            best_reduced_cost,
            best_waiting_cost,
            labels_processed,
            labels_created,
            max_frontier,
        )


def price_station(
    candidates: list[Candidate],
    capacity: int,
    initial_available: Iterable[float],
    station_dual: float,
    customer_duals: dict[int, float],
    tolerance: float = 1e-8,
) -> tuple[PricingResult, str]:
    """Return the minimum-reduced-cost sequence, including the empty column."""
    ordered = _sort_full_information(candidates)
    initial = tuple(sorted(float(value) for value in initial_available))
    if capacity <= 0 or len(initial) != capacity:
        raise ValueError("initial_available length must equal positive station capacity.")
    empty = PricingResult(tuple(), 0.0, -float(station_dual), 0, 0, 0)
    if not ordered:
        return empty, "none"

    for item in ordered:
        if item.request_id not in customer_duals:
            raise KeyError(f"Missing customer dual for request {item.request_id}.")

    if njit is not None:
        arrivals = np.asarray([item.arrival_time for item in ordered], dtype=np.float64)
        durations = np.asarray([item.duration for item in ordered], dtype=np.float64)
        duals = np.asarray(
            [customer_duals[item.request_id] for item in ordered], dtype=np.float64
        )
        initial_array = np.asarray(initial, dtype=np.float64)
        raw = _price_core_numba(
            arrivals,
            durations,
            duals,
            initial_array,
            float(station_dual),
            float(tolerance),
        )
        indices, reduced_cost, waiting_cost, processed, created, max_frontier = raw
        if len(indices) == 0:
            return empty, "numba"
        nonempty = PricingResult(
            sequence=tuple(ordered[int(index)].request_id for index in indices),
            waiting_cost=float(waiting_cost),
            reduced_cost=float(reduced_cost),
            labels_processed=int(processed),
            labels_created=int(created),
            max_labels_at_vertex=int(max_frontier),
        )
        return (nonempty if nonempty.reduced_cost < empty.reduced_cost - tolerance else empty), "numba"

    return price_station_python(
        ordered,
        capacity,
        initial,
        station_dual,
        customer_duals,
        tolerance,
    ), "python"


def _warm_up_numba() -> float:
    if njit is None:
        return 0.0
    start = time.perf_counter()
    _price_core_numba(
        np.asarray([0.0], dtype=np.float64),
        np.asarray([1.0], dtype=np.float64),
        np.asarray([0.0], dtype=np.float64),
        np.asarray([0.0], dtype=np.float64),
        0.0,
        1e-8,
    )
    return time.perf_counter() - start


def solve_full_information(
    requests: list[Request],
    stations: list[Station],
    travel_slots_per_segment: float,
    output_flag: int = 1,
    tolerance: float = 1e-7,
    max_iterations: int = 10000,
    time_limit: float | None = None,
) -> dict:
    try:
        import gurobipy as gp
        from gurobipy import GRB
    except ImportError as exc:
        raise RuntimeError(
            "gurobipy is required for the restricted master problem. Install Gurobi and activate a valid license."
        ) from exc

    if not requests:
        raise ValueError("The selected realized-scenario worksheet contains no requests.")
    if not stations:
        raise ValueError("At least one charging station is required.")
    if not math.isfinite(travel_slots_per_segment) or travel_slots_per_segment <= 0:
        raise ValueError("travel_slots_per_segment must be positive and finite.")
    if tolerance <= 0 or not math.isfinite(tolerance):
        raise ValueError("tolerance must be positive and finite.")
    if max_iterations <= 0:
        raise ValueError("max_iterations must be positive.")
    if time_limit is not None and time_limit <= 0:
        raise ValueError("time_limit must be positive when supplied.")

    request_ids = [request.request_id for request in requests]
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("Request IDs must be unique within the realized scenario.")

    candidates_by_station: list[list[Candidate]] = [[] for _ in stations]
    eligible_by_request: dict[int, list[int]] = {rid: [] for rid in request_ids}
    candidate_by_pair: dict[tuple[int, int], Candidate] = {}

    for request in requests:
        for station_index, station in enumerate(stations):
            if request.origin_segment - 1e-9 <= station.segment <= request.max_segment + 1e-9:
                candidate = make_candidate(request, station, travel_slots_per_segment)
                # In [DP], every request in the realized scenario appears with certainty.
                candidate = Candidate(
                    request_id=candidate.request_id,
                    arrival_time=candidate.arrival_time,
                    duration=candidate.duration,
                    probability=1.0,
                )
                candidates_by_station[station_index].append(candidate)
                eligible_by_request[request.request_id].append(station_index)
                candidate_by_pair[(request.request_id, station_index)] = candidate
        if not eligible_by_request[request.request_id]:
            raise ValueError(
                f"Realized request {request.request_id} cannot reach any configured station."
            )

    for station_index in range(len(stations)):
        candidates_by_station[station_index] = _sort_full_information(
            candidates_by_station[station_index]
        )

    # Construct a deterministic feasible starting partition. The seed affects
    # only the number of CG iterations, not the converged LP optimum.
    initial_sequences: list[list[int]] = [[] for _ in stations]
    for request in sorted(requests, key=lambda item: item.request_id):
        station_index = min(
            eligible_by_request[request.request_id],
            key=lambda m: (candidate_by_pair[(request.request_id, m)].arrival_time, m),
        )
        initial_sequences[station_index].append(request.request_id)
    for station_index in range(len(stations)):
        initial_sequences[station_index].sort(
            key=lambda rid: (candidate_by_pair[(rid, station_index)].arrival_time, rid)
        )

    numba_warmup_seconds = _warm_up_numba()
    wall_start = time.perf_counter()
    model = gp.Model("Full_Information_Column_Generation")
    model.ModelSense = GRB.MINIMIZE
    model.Params.OutputFlag = int(output_flag)
    model.Params.Method = 1  # Dual simplex is suitable for repeated column additions.

    station_constraints = {
        m: model.addConstr(gp.LinExpr() == 1.0, name=f"station_{m}")
        for m in range(len(stations))
    }
    customer_constraints = {
        request.request_id: model.addConstr(
            gp.LinExpr() == 1.0, name=f"request_{request.request_id}"
        )
        for request in requests
    }
    model.update()

    columns_by_station: list[list[tuple[tuple[int, ...], float, object]]] = [
        [] for _ in stations
    ]
    column_keys: list[set[tuple[int, ...]]] = [set() for _ in stations]

    def add_column(station_index: int, sequence: tuple[int, ...], cost: float) -> bool:
        sequence = tuple(sequence)
        if sequence in column_keys[station_index]:
            return False
        if len(sequence) != len(set(sequence)):
            raise ValueError("A service sequence cannot contain a request more than once.")
        for request_id in sequence:
            if (request_id, station_index) not in candidate_by_pair:
                raise ValueError(
                    f"Request {request_id} is not accessible to station {stations[station_index].station_id}."
                )
        column = gp.Column()
        column.addTerms(1.0, station_constraints[station_index])
        for request_id in sequence:
            column.addTerms(1.0, customer_constraints[request_id])
        variable = model.addVar(
            lb=0.0,
            ub=1.0,
            obj=float(cost),
            vtype=GRB.CONTINUOUS,
            column=column,
            name=f"x_{station_index}_{len(columns_by_station[station_index])}",
        )
        columns_by_station[station_index].append((sequence, float(cost), variable))
        column_keys[station_index].add(sequence)
        return True

    for station_index in range(len(stations)):
        add_column(station_index, tuple(), 0.0)

    # Add one feasible partition to make the initial restricted master feasible.
    for station_index, request_ids_for_station in enumerate(initial_sequences):
        sequence = tuple(request_ids_for_station)
        cost = deterministic_waiting_cost(
            [candidate_by_pair[(rid, station_index)] for rid in sequence],
            capacity=stations[station_index].capacity,
            initial_available=stations[station_index].initial_available,
        )
        add_column(station_index, sequence, cost)
    model.update()

    converged = False
    iteration = 0
    last_min_reduced_cost = math.inf
    labels_processed = 0
    labels_created = 0
    max_labels_at_vertex = 0
    pricing_backends: set[str] = set()
    added_columns_total = 0

    try:
        while iteration < max_iterations:
            iteration += 1
            if time_limit is not None:
                remaining = time_limit - (time.perf_counter() - wall_start)
                if remaining <= 0:
                    raise TimeoutError(
                        "Column generation reached the global time limit before proving LP optimality."
                    )
                model.Params.TimeLimit = remaining

            model.optimize()
            if model.Status != GRB.OPTIMAL:
                raise RuntimeError(
                    "Restricted master problem was not solved to optimality "
                    f"(Gurobi status {model.Status}); the full-information LP bound is not certified."
                )

            station_duals = {
                m: float(station_constraints[m].Pi) for m in range(len(stations))
            }
            customer_duals = {
                rid: float(customer_constraints[rid].Pi)
                for rid in customer_constraints
            }

            added_this_iteration = 0
            reduced_costs_this_iteration: list[float] = []

            for station_index, station in enumerate(stations):
                if time_limit is not None and time.perf_counter() - wall_start >= time_limit:
                    raise TimeoutError(
                        "Column generation reached the global time limit during pricing; "
                        "LP optimality has not been certified."
                    )
                pricing, backend = price_station(
                    candidates_by_station[station_index],
                    capacity=station.capacity,
                    initial_available=station.initial_available,
                    station_dual=station_duals[station_index],
                    customer_duals=customer_duals,
                    tolerance=tolerance,
                )
                pricing_backends.add(backend)
                reduced_costs_this_iteration.append(pricing.reduced_cost)
                labels_processed += pricing.labels_processed
                labels_created += pricing.labels_created
                max_labels_at_vertex = max(
                    max_labels_at_vertex, pricing.max_labels_at_vertex
                )

                if pricing.reduced_cost < -tolerance:
                    if pricing.sequence in column_keys[station_index]:
                        raise RuntimeError(
                            "Pricing returned an existing column with negative reduced cost. "
                            "Check RMP dual feasibility and numerical tolerances."
                        )
                    if add_column(
                        station_index,
                        pricing.sequence,
                        pricing.waiting_cost,
                    ):
                        added_this_iteration += 1
                        added_columns_total += 1

            last_min_reduced_cost = min(reduced_costs_this_iteration)
            if added_this_iteration == 0:
                converged = True
                break
            model.update()

        wall_time = time.perf_counter() - wall_start
        if not converged:
            raise RuntimeError(
                f"Column generation reached max_iterations={max_iterations} without proving LP optimality."
            )

        positive_columns = []
        for station_index, station_columns in enumerate(columns_by_station):
            for sequence, cost, variable in station_columns:
                if variable.X > 1e-8:
                    positive_columns.append(
                        {
                            "station": stations[station_index].station_id,
                            "sequence": list(sequence),
                            "column_cost": cost,
                            "weight": float(variable.X),
                        }
                    )

        result = {
            "status": int(model.Status),
            "status_name": "OPTIMAL_LP_RELAXATION",
            "objective_lower_bound": float(model.ObjVal),
            "runtime_seconds": wall_time,
            "numba_warmup_seconds": numba_warmup_seconds,
            "column_generation_iterations": iteration,
            "total_columns": sum(len(columns) for columns in columns_by_station),
            "columns_added_after_initialization": added_columns_total,
            "columns_by_station": {
                stations[m].station_id: len(columns_by_station[m])
                for m in range(len(stations))
            },
            "minimum_reduced_cost_at_termination": float(last_min_reduced_cost),
            "pricing_backend": ",".join(sorted(pricing_backends)),
            "pricing_label_statistics": {
                "labels_processed": labels_processed,
                "labels_created": labels_created,
                "maximum_labels_at_vertex": max_labels_at_vertex,
            },
            "initialization_method": "nearest_accessible_station_greedy",
            "positive_master_columns": positive_columns,
        }
        return result
    finally:
        model.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Solve the full-information LP relaxation [DP] by exact column generation."
    )
    parser.add_argument("--input", required=True, help="Workbook containing a realized scenario (.xlsx or .xls).")
    parser.add_argument("--sheet", required=True, help="Realized-scenario worksheet, e.g. Scenario_1.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--station-config", help="Station JSON configuration.")
    group.add_argument("--station-sheet", help="Station worksheet, e.g. CS.")
    parser.add_argument(
        "--station-workbook",
        default=None,
        help="Workbook containing the station worksheet. Defaults to --input.",
    )
    parser.add_argument("--travel-scale", type=float, default=None)
    parser.add_argument("--max-iterations", type=int, default=10000)
    parser.add_argument("--time-limit", type=float, default=None)
    parser.add_argument("--tolerance", type=float, default=1e-7)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    requests = load_requests_xlsx(args.input, args.sheet)
    if args.station_config:
        stations, config_travel_scale = load_station_config(args.station_config)
        travel_scale = args.travel_scale or config_travel_scale
    else:
        station_workbook = args.station_workbook or args.input
        stations = load_station_sheet_xlsx(station_workbook, args.station_sheet)
        travel_scale = args.travel_scale or 2.0

    result = solve_full_information(
        requests,
        stations,
        travel_slots_per_segment=travel_scale,
        output_flag=0 if args.quiet else 1,
        tolerance=args.tolerance,
        max_iterations=args.max_iterations,
        time_limit=args.time_limit,
    )
    result["input_workbook"] = str(args.input)
    result["input_sheet"] = args.sheet
    result["station_workbook"] = args.station_workbook or args.input
    result["number_of_realized_requests"] = len(requests)
    result["travel_slots_per_segment"] = travel_scale
    rendered = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
