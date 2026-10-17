"""Many-objective optimization: the trade-off frontier between several metrics.

A weighted sum picks one point of the trade-off. With ``pareto = [...]`` the optimizer
instead runs once per weight vector spread evenly over the listed metrics (a simplex
lattice, extremes included) and keeps every fit that no other fit beats on all of
them at once: the Pareto front. Each run is an ordinary optimization, so the
constraints and fitting choices of the spec still apply to every point.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass

from evesim.data.store import StaticData
from evesim.dogma.engine import Character
from evesim.market import Prices
from evesim.optimize.search import Evaluation, Optimizer
from evesim.optimize.spec import METRICS, Spec


@dataclass
class ParetoPoint:
    ship: str
    weights: dict[str, float]
    evaluation: Evaluation

    @property
    def metrics(self) -> dict[str, float]:
        return self.evaluation.metrics


def weight_vectors(n_metrics: int, points: int) -> list[tuple[float, ...]]:
    """Evenly spread weight vectors on the simplex, extremes included, at least ``points`` of them."""
    if n_metrics == 1:
        return [(1.0,)]
    h = 1
    while math.comb(h + n_metrics - 1, n_metrics - 1) < points:
        h += 1

    def parts(total, k):
        if k == 1:
            yield (total,)
            return
        for i in range(total + 1):
            for rest in parts(total - i, k - 1):
                yield (i,) + rest

    return [tuple(p / h for p in v) for v in parts(h, n_metrics)]


def dominates(a: dict[str, float], b: dict[str, float], metrics: list[str]) -> bool:
    better = False
    for m in metrics:
        va, vb = a[m], b[m]
        if not METRICS[m].higher_is_better:
            va, vb = -va, -vb
        if va < vb - 1e-9:
            return False
        if va > vb + 1e-9:
            better = True
    return better


def front(points: list[ParetoPoint], metrics: list[str]) -> list[ParetoPoint]:
    out = []
    for p in points:
        if not any(dominates(q.metrics, p.metrics, metrics) for q in points if q is not p):
            out.append(p)
    return out


def run(data: StaticData, char: Character, spec: Spec, prices: Prices | None = None,
        log=lambda m: None) -> list[ParetoPoint]:
    metrics = spec.pareto
    collected: list[ParetoPoint] = []
    seen: set[tuple] = set()
    vectors = weight_vectors(len(metrics), spec.pareto_points)
    for i, vec in enumerate(vectors, 1):
        weights = {m: w for m, w in zip(metrics, vec) if w > 0}
        log(f"pareto point {i}/{len(vectors)}: " + ", ".join(f"{m} {w:.2f}" for m, w in weights.items()))
        sub = copy.copy(spec)
        sub.weights = weights
        sub.pareto = []
        opt = Optimizer(data, char, sub, prices)
        for res in opt.run():
            for ev in ([res.best] if res.best else []) + res.alternatives:
                if not ev.feasible:
                    continue
                key = opt._key(ev.fit)
                if key in seen:
                    continue
                seen.add(key)
                # Make sure every Pareto metric is present, whatever this run's weights were.
                for m in metrics:
                    if m not in ev.metrics:
                        ev.metrics[m] = METRICS[m].fn(ev.stats, spec)
                collected.append(ParetoPoint(res.ship.name, weights, ev))
    result = front(collected, metrics)
    result.sort(key=lambda p: -p.metrics[metrics[0]] if METRICS[metrics[0]].higher_is_better else p.metrics[metrics[0]])
    return result
