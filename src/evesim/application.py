"""Damage application against a target: turret hit chance, missile explosion
formula, target resists.

The engagement is described by a :class:`Target`: its signature radius, speed,
the distance it is held at and the share of its speed that is transversal.
Drones are assumed to apply fully unless the target outruns them, a deliberate
simplification (their real behaviour depends on drone AI and orbit).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from evesim.dogma import const as C
from evesim.stats import Stats, Weapon


@dataclass
class Target:
    name: str = "target"
    signature: float = 125.0          # m
    velocity: float = 0.0             # m/s
    distance: float = 10_000.0        # m
    transversal_share: float = 1.0    # 1 = orbiting / fully transversal, 0 = head-on
    resonance: dict[str, float] = field(default_factory=lambda: {k: 1.0 for k in C.DAMAGE_TYPES})

    @property
    def angular(self) -> float:
        return self.velocity * self.transversal_share / max(self.distance, 1.0)


# Rough presets for common engagements; resonances default to no resist.
PRESETS = {
    "frigate": Target("frigate", 35, 400, 5_000),
    "destroyer": Target("destroyer", 70, 300, 8_000),
    "cruiser": Target("cruiser", 125, 250, 15_000),
    "battlecruiser": Target("battlecruiser", 270, 180, 20_000),
    "battleship": Target("battleship", 400, 120, 30_000),
    "structure": Target("structure", 10_000, 0, 30_000),
}


def turret_factor(w: Weapon, t: Target) -> float:
    """Expected damage multiplier per shot, including wrecking hits."""
    if w.tracking <= 0:
        tracking_term = 0.0
    else:
        tracking_term = (t.angular * 40_000.0 / (w.tracking * max(t.signature, 1.0))) ** 2
    range_term = (max(0.0, t.distance - w.optimal) / w.falloff) ** 2 if w.falloff > 0 else \
        (0.0 if t.distance <= w.optimal else math.inf)
    cth = 0.5 ** (tracking_term + range_term)
    if cth <= 0.01:
        return cth * 3.0
    return 0.03 + (cth - 0.01) * (0.49 + (cth + 0.01) / 2.0)


def missile_factor(w: Weapon, t: Target) -> float:
    if t.distance > w.optimal:
        return 0.0
    if w.explosion_radius <= 0:
        return 1.0
    sig_ratio = t.signature / w.explosion_radius
    if t.velocity <= 0 or w.explosion_velocity <= 0:
        return min(1.0, sig_ratio)
    drf = w.drf if w.drf > 0 else 1.0
    vel_term = (sig_ratio * w.explosion_velocity / t.velocity) ** drf
    return min(1.0, sig_ratio, vel_term)


def drone_factor(w: Weapon, t: Target) -> float:
    if w.velocity and t.velocity > w.velocity:
        return 0.0
    return 1.0


def weapon_factor(w: Weapon, t: Target) -> float:
    if w.kind in ("turret", "disintegrator"):
        return turret_factor(w, t)
    if w.kind == "missile":
        return missile_factor(w, t)
    if w.kind == "vorton":
        return missile_factor(w, t) if t.distance <= w.optimal else 0.0
    if w.kind == "smartbomb":
        return 1.0 if t.distance <= w.optimal else 0.0
    if w.kind == "drone":
        return drone_factor(w, t)
    return 1.0


def applied_dps(stats: Stats, t: Target) -> float:
    """DPS that lands on the target after application and its resists."""
    total = 0.0
    for w in stats.weapons:
        f = weapon_factor(w, t)
        if f <= 0:
            continue
        by_type = w.dps_by_type()
        total += f * sum(by_type[k] * t.resonance.get(k, 1.0) for k in C.DAMAGE_TYPES)
    return total


def dps_curve(stats: Stats, t: Target, distances: list[float]) -> list[tuple[float, float]]:
    out = []
    for d in distances:
        tt = Target(t.name, t.signature, t.velocity, d, t.transversal_share, t.resonance)
        out.append((d, applied_dps(stats, tt)))
    return out
