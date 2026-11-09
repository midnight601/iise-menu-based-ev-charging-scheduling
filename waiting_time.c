#include <math.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>

#ifdef _WIN32
#define WT_EXPORT __declspec(dllexport)
#else
#define WT_EXPORT
#endif

typedef struct {
    uint64_t hash;
    size_t index_plus_one;
} HashEntry;

static uint64_t hash_state(const double *state, int capacity) {
    uint64_t hash = UINT64_C(1469598103934665603);
    for (int k = 0; k < capacity; ++k) {
        double value = (state[k] == 0.0) ? 0.0 : state[k];
        uint64_t bits = 0;
        memcpy(&bits, &value, sizeof(bits));
        for (int b = 0; b < 8; ++b) {
            hash ^= (uint8_t)(bits & UINT64_C(0xff));
            hash *= UINT64_C(1099511628211);
            bits >>= 8;
        }
    }
    return hash;
}

static int same_state(const double *a, const double *b, int capacity) {
    for (int k = 0; k < capacity; ++k) {
        if (a[k] != b[k]) return 0;
    }
    return 1;
}

static size_t next_power_of_two(size_t value) {
    size_t result = 1;
    while (result < value) {
        if (result > SIZE_MAX / 2) return 0;
        result <<= 1;
    }
    return result;
}

static void sort_ascending(double *values, int length) {
    for (int i = 1; i < length; ++i) {
        double value = values[i];
        int j = i - 1;
        while (j >= 0 && values[j] > value) {
            values[j + 1] = values[j];
            --j;
        }
        values[j + 1] = value;
    }
}

/* Insert a state into an open-addressed hash table, merging identical states. */
static int insert_state(
    const double *state,
    double mass,
    int capacity,
    double *states,
    double *masses,
    size_t *state_count,
    size_t state_capacity,
    HashEntry *table,
    size_t table_size
) {
    if (mass == 0.0) return 0;

    const uint64_t hash = hash_state(state, capacity);
    size_t position = (size_t)hash & (table_size - 1);

    while (table[position].index_plus_one != 0) {
        const size_t existing_index = table[position].index_plus_one - 1;
        if (table[position].hash == hash &&
            same_state(states + existing_index * (size_t)capacity, state, capacity)) {
            masses[existing_index] += mass;
            return 0;
        }
        position = (position + 1) & (table_size - 1);
    }

    if (*state_count >= state_capacity) return 5;

    const size_t new_index = *state_count;
    memcpy(states + new_index * (size_t)capacity,
           state,
           (size_t)capacity * sizeof(double));
    masses[new_index] = mass;
    table[position].hash = hash;
    table[position].index_plus_one = new_index + 1;
    *state_count += 1;
    return 0;
}


WT_EXPORT int expected_conditional_waits(
    int num_customers,
    int num_chargers,
    const double *probabilities,
    const double *durations,
    const double *arrival_times,
    const double *initial_available,
    double *expected_waits,
    size_t *max_states_out
) {
    if (num_customers < 0 || num_chargers <= 0) return 1;
    if (num_customers > 0 &&
        (probabilities == NULL || durations == NULL || arrival_times == NULL ||
         expected_waits == NULL)) return 1;
    if (initial_available == NULL) return 1;

    if (max_states_out != NULL) *max_states_out = 1;
    if (num_customers == 0) return 0;

    for (int i = 0; i < num_customers; ++i) {
        if (!isfinite(probabilities[i]) || probabilities[i] < 0.0 || probabilities[i] > 1.0 ||
            !isfinite(durations[i]) || durations[i] < 0.0 ||
            !isfinite(arrival_times[i])) return 2;
    }
    for (int k = 0; k < num_chargers; ++k) {
        if (!isfinite(initial_available[k])) return 2;
    }

    if ((size_t)num_chargers > SIZE_MAX / sizeof(double)) return 4;

    double *current_states = (double *)malloc((size_t)num_chargers * sizeof(double));
    double *current_masses = (double *)malloc(sizeof(double));
    if (current_states == NULL || current_masses == NULL) {
        free(current_states);
        free(current_masses);
        return 3;
    }

    memcpy(current_states, initial_available, (size_t)num_chargers * sizeof(double));
    sort_ascending(current_states, num_chargers);
    current_masses[0] = 1.0;
    size_t current_count = 1;
    size_t max_states = 1;
    int status = 0;

    for (int i = 0; i < num_customers; ++i) {
        long double expected_wait = 0.0L;
        for (size_t s = 0; s < current_count; ++s) {
            const double *state = current_states + s * (size_t)num_chargers;
            double wait = state[0] - arrival_times[i];
            if (wait < 0.0) wait = 0.0;
            expected_wait += (long double)current_masses[s] * (long double)wait;
        }
        expected_waits[i] = (double)expected_wait;

        if (i == num_customers - 1) break;

        if (current_count > SIZE_MAX / 2) {
            status = 4;
            break;
        }
        const size_t next_capacity = current_count * 2;
        if (next_capacity > SIZE_MAX / (size_t)num_chargers ||
            next_capacity * (size_t)num_chargers > SIZE_MAX / sizeof(double)) {
            status = 4;
            break;
        }
        if (next_capacity > SIZE_MAX / 2) {
            status = 4;
            break;
        }
        const size_t table_size = next_power_of_two(next_capacity * 2);
        if (table_size == 0 || table_size > SIZE_MAX / sizeof(HashEntry)) {
            status = 4;
            break;
        }

        double *next_states = (double *)malloc(
            next_capacity * (size_t)num_chargers * sizeof(double));
        double *next_masses = (double *)malloc(next_capacity * sizeof(double));
        HashEntry *table = (HashEntry *)calloc(table_size, sizeof(HashEntry));
        double *updated_state = (double *)malloc((size_t)num_chargers * sizeof(double));

        if (next_states == NULL || next_masses == NULL || table == NULL || updated_state == NULL) {
            free(next_states);
            free(next_masses);
            free(table);
            free(updated_state);
            status = 3;
            break;
        }

        size_t next_count = 0;
        const double p = probabilities[i];

        for (size_t s = 0; s < current_count && status == 0; ++s) {
            const double *state = current_states + s * (size_t)num_chargers;
            const double mass = current_masses[s];

            /* The request does not appear. */
            if (p < 1.0) {
                status = insert_state(
                    state, mass * (1.0 - p), num_chargers,
                    next_states, next_masses, &next_count, next_capacity,
                    table, table_size);
                if (status != 0) break;
            }

            /* The request appears and occupies the earliest available charger. */
            if (p > 0.0) {
                const double service_start =
                    (state[0] > arrival_times[i]) ? state[0] : arrival_times[i];
                const double completion = service_start + durations[i];

                for (int k = 0; k < num_chargers - 1; ++k) {
                    updated_state[k] = state[k + 1];
                }
                updated_state[num_chargers - 1] = completion;
                sort_ascending(updated_state, num_chargers);

                status = insert_state(
                    updated_state, mass * p, num_chargers,
                    next_states, next_masses, &next_count, next_capacity,
                    table, table_size);
                if (status != 0) break;
            }
        }

        free(table);
        free(updated_state);

        if (status != 0) {
            free(next_states);
            free(next_masses);
            break;
        }

        free(current_states);
        free(current_masses);
        current_states = next_states;
        current_masses = next_masses;
        current_count = next_count;
        if (current_count > max_states) max_states = current_count;
    }

    free(current_states);
    free(current_masses);
    if (max_states_out != NULL) *max_states_out = max_states;
    return status;
}
