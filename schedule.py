"""Minimize the busiest worker's time plus 2000 ms per nonempty worker."""


OVERHEAD = 2_000


def two_way(tests, weights, limit):
    # Bit i indicates that a subset of the processed tests has load i.
    # Keep each prefix so we can reconstruct the best split.
    target = sum(weights[test] for test in tests) // 2
    mask = (1 << (target + 1)) - 1
    possible = [1]
    for test in tests:
        bits = possible[-1]
        possible.append(bits | ((bits << weights[test]) & mask))
    half = possible[-1].bit_length() - 1
    peak = sum(weights[test] for test in tests) - half
    if peak >= limit:
        return limit, None
    first, second = [], []
    for index in range(len(tests) - 1, -1, -1):
        test = tests[index]
        if (possible[index] >> half) & 1:
            second.append(test)
        else:
            first.append(test)
            half -= weights[test]
    return peak, [first, second]


def schedule(selection, weights):
    if not selection:
        return 0, []
    tests = sorted(selection, key=lambda test: (-weights[test], test))
    total = sum(weights[test] for test in tests)
    largest = weights[tests[0]]
    best_cost, best_workers = total + OVERHEAD, [tests]
    for count in range(2, min(8, len(tests)) + 1):
        lower_bound = max(largest, (total + count - 1) // count)
        if largest + OVERHEAD * count >= best_cost:
            break
        if lower_bound + OVERHEAD * count >= best_cost:
            continue
        workers, loads = [[] for _ in range(count)], [0] * count
        for test in tests:
            worker = min(range(count), key=lambda index: (loads[index], len(workers[index])))
            workers[worker].append(test)
            loads[worker] += weights[test]
        peak = max(loads)
        if count == 2 and peak > lower_bound:
            improved, assignment = two_way(tests, weights, peak)
            if assignment:
                peak, workers = improved, assignment
        cost = peak + OVERHEAD * count
        if cost < best_cost:
            best_cost, best_workers = cost, workers
    return best_cost, best_workers
