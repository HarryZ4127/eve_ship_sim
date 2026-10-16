"""Optimization requirements: what to maximize, hard constraints, and what may be fitted.

A spec is a TOML file (or dict) like::

    ship = "Caracal"

    [objective]
    maximize = "applied_dps"        # or a weighted mix under [objective.weights]

    [target]                        # who we shoot (applied_dps)
    preset = "cruiser"
    distance = 25000

    [incoming]                      # damage we expect to take (ehp / tank metrics)
    preset = "guristas"

    [constraints]
    min_ehp = 15000
    cap_stable = true
    max_price = 80_000_000

    [fitting]
    meta = ["tech1", "tech2", "storyline"]
    tank = "shield"
    propulsion = "ab"
    require = ["Warp Scrambler II"]

See ``docs/requirements.md`` for every field.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from evesim.application import PRESETS as TARGET_PRESETS, Target, applied_dps
from evesim.stats import Stats

META_GROUPS = {"tech1": {None, 1}, "tech2": {2}, "storyline": {3}, "faction": {4}, "officer": {5},
               "deadspace": {6}, "tech3": {14}}
DEFAULT_META = ["tech1", "tech2", "storyline"]
DEFAULT_AMMO_META = ["tech1", "tech2", "storyline", "faction"]

# Incoming damage profiles (em, thermal, kinetic, explosive) for common NPC factions.
DAMAGE_PROFILES = {
    "uniform": (25, 25, 25, 25),
    "em": (100, 0, 0, 0), "thermal": (0, 100, 0, 0), "kinetic": (0, 0, 100, 0), "explosive": (0, 0, 0, 100),
    "guristas": (0, 18, 82, 0), "serpentis": (0, 45, 55, 0), "blood": (50, 48, 2, 0),
    "sansha": (53, 47, 0, 0), "angel": (7, 9, 22, 62), "drones": (0, 24, 38, 38),
    "mordus": (0, 25, 75, 0), "triglavian": (0, 40, 0, 60), "edencom": (40, 30, 30, 0),
}


@dataclass(frozen=True)
class Metric:
    name: str
    higher_is_better: bool
    fn: Callable[[Stats, "Spec"], float]
    unit: str = ""
    description: str = ""


def _optimal_range(s: Stats, _spec) -> float:
    best = 0.0
    for w in s.weapons:
        if w.kind == "drone":
            continue
        r = w.optimal if w.kind in ("missile", "vorton", "smartbomb") else w.optimal + w.falloff
        best = max(best, r)
    return best


def _cap_seconds(s: Stats, _spec) -> float:
    return math.inf if s.cap.stable else (s.cap.lasts_s or 0.0)


METRICS: dict[str, Metric] = {m.name: m for m in [
    Metric("dps", True, lambda s, sp: s.dps, "HP/s", "raw DPS of weapons and drones"),
    Metric("weapon_dps", True, lambda s, sp: s.weapon_dps, "HP/s", "raw DPS without drones"),
    Metric("drone_dps", True, lambda s, sp: s.drone_dps, "HP/s", "raw drone DPS"),
    Metric("applied_dps", True, lambda s, sp: applied_dps(s, sp.target), "HP/s",
           "DPS landing on [target] after tracking/explosion and its resists"),
    Metric("volley", True, lambda s, sp: s.volley, "HP", "alpha strike"),
    Metric("ehp", True, lambda s, sp: s.ehp_total, "HP", "effective HP against [incoming]"),
    Metric("shield_ehp", True, lambda s, sp: s.ehp["shield"], "HP"),
    Metric("armor_ehp", True, lambda s, sp: s.ehp["armor"], "HP"),
    Metric("sustained_tank", True, lambda s, sp: s.sustained_ehps_reload, "EHP/s",
           "cap-limited repair + passive regen against [incoming], ancillary reloads included"),
    Metric("peak_tank", True, lambda s, sp: s.peak_ehps, "EHP/s"),
    Metric("speed", True, lambda s, sp: s.nav["max_velocity"], "m/s"),
    Metric("align_time", False, lambda s, sp: s.nav["align_time"], "s"),
    Metric("signature", False, lambda s, sp: s.nav["signature_radius"], "m"),
    Metric("lock_range", True, lambda s, sp: s.targeting["lock_range"], "m"),
    Metric("scan_resolution", True, lambda s, sp: s.targeting["scan_resolution"], "mm"),
    Metric("weapon_range", True, _optimal_range, "m", "longest optimal+falloff / missile flight range"),
    Metric("cap_seconds", True, _cap_seconds, "s", "time until cap runs dry (inf if stable)"),
    Metric("cap_stable_fraction", True, lambda s, sp: s.cap.stable_fraction, ""),
    Metric("cap_delta", True, lambda s, sp: s.cap.delta_per_s, "GJ/s", "peak recharge minus average use"),
    Metric("price", False, lambda s, sp: s.price or 0.0, "ISK"),
    Metric("cpu_left", True, lambda s, sp: s.resources["cpu"][1] - s.resources["cpu"][0], "tf"),
    Metric("pg_left", True, lambda s, sp: s.resources["power"][1] - s.resources["power"][0], "MW"),
]}

# Dogma attribute names each metric depends on; used to skip modules that cannot matter.
METRIC_ATTRS: dict[str, set[str]] = {
    "dps": {"damageMultiplier", "speed", "emDamage", "thermalDamage", "kineticDamage", "explosiveDamage",
            "missileDamageMultiplier", "maxActiveDrones"},
    "volley": {"damageMultiplier", "emDamage", "thermalDamage", "kineticDamage", "explosiveDamage",
               "missileDamageMultiplier"},
    "application": {"trackingSpeed", "maxRange", "falloff", "aoeCloudSize", "aoeVelocity", "maxVelocity",
                    "explosionDelay", "aoeDamageReductionFactor"},
    "ehp": {"shieldCapacity", "armorHP", "hp", "shieldEmDamageResonance", "shieldThermalDamageResonance",
            "shieldKineticDamageResonance", "shieldExplosiveDamageResonance", "armorEmDamageResonance",
            "armorThermalDamageResonance", "armorKineticDamageResonance", "armorExplosiveDamageResonance",
            "emDamageResonance", "thermalDamageResonance", "kineticDamageResonance", "explosiveDamageResonance"},
    "tank": {"shieldBonus", "armorDamageAmount", "structureDamageAmount", "duration", "shieldRechargeRate",
             "chargedArmorDamageMultiplier", "capacitorNeed"},
    "cap": {"capacitorCapacity", "rechargeRate", "capacitorNeed", "duration", "speed", "capacitorBonus"},
    "nav": {"maxVelocity", "mass", "agility", "signatureRadius", "speedFactor", "speedBoostFactor",
            "massAddition", "signatureRadiusBonus"},
    "targeting": {"maxTargetRange", "scanResolution"},
    "fitting": {"cpuOutput", "powerOutput", "upgradeCapacity", "cpu", "power", "upgradeCost"},
}
METRIC_GROUPS = {
    "dps": ["dps"], "weapon_dps": ["dps"], "drone_dps": ["dps"], "volley": ["volley"],
    "applied_dps": ["dps", "application"], "weapon_range": ["application"],
    "ehp": ["ehp"], "shield_ehp": ["ehp"], "armor_ehp": ["ehp"],
    "sustained_tank": ["ehp", "tank", "cap"], "peak_tank": ["ehp", "tank"],
    "speed": ["nav"], "align_time": ["nav"], "signature": ["nav"],
    "lock_range": ["targeting"], "scan_resolution": ["targeting"],
    "cap_seconds": ["cap"], "cap_stable_fraction": ["cap"], "cap_delta": ["cap"],
    "price": [], "cpu_left": ["fitting"], "pg_left": ["fitting"],
}


@dataclass
class Constraint:
    metric: str
    bound: float
    kind: str  # "min" or "max"

    def value(self, s: Stats, spec: "Spec") -> float:
        return METRICS[self.metric].fn(s, spec)

    def shortfall(self, v: float) -> float:
        """Relative violation (0 when satisfied)."""
        scale = max(abs(self.bound), 1e-9)
        if self.kind == "min":
            return 0.0 if v >= self.bound else min((self.bound - v) / scale, 10.0)
        return 0.0 if v <= self.bound else min((v - self.bound) / scale, 10.0)

    def __str__(self):
        return f"{self.metric} {'>=' if self.kind == 'min' else '<='} {self.bound:g}"


@dataclass
class Spec:
    ships: list[str] = field(default_factory=list)
    ship_groups: list[str] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=lambda: {"dps": 1.0})
    scales: dict[str, float] = field(default_factory=dict)
    constraints: list[Constraint] = field(default_factory=list)
    target: Target = field(default_factory=lambda: TARGET_PRESETS["cruiser"])
    incoming: dict[str, float] = field(default_factory=lambda: {"em": 25, "thermal": 25, "kinetic": 25, "explosive": 25})
    meta: list[str] = field(default_factory=lambda: list(DEFAULT_META))
    ammo_meta: list[str] = field(default_factory=lambda: list(DEFAULT_AMMO_META))
    require: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    tank: str = "any"            # shield | armor | hull | any
    propulsion: str = "any"      # mwd | ab | none | any
    weapons: str = "any"         # turret | missile | drone | none | any
    weapon: str | None = None    # pin a weapon module by name
    ammo: list[str] = field(default_factory=list)  # allowed charges by name (empty = any)
    drones: bool = True
    oversized_prop: bool = False
    smartbombs: bool = False     # smartbombs also hit your own drones; off by default
    fill_slots: bool = True      # fill slots the objective leaves empty with EHP/DPS
    subsystems: list[str] = field(default_factory=list)
    cpu_margin: float = 0.0      # keep this much CPU free
    pg_margin: float = 0.0
    effort: str = "normal"       # quick | normal | thorough
    seed: int = 1
    name: str = ""

    @property
    def cap_simulation_needed(self) -> bool:
        names = set(self.weights) | {c.metric for c in self.constraints}
        return bool(names & {"cap_seconds", "cap_stable_fraction", "sustained_tank"})

    def relevant_metrics(self) -> set[str]:
        return {m for m, w in self.weights.items() if w} | {c.metric for c in self.constraints}

    def relevant_attrs(self) -> set[str]:
        out = set(METRIC_ATTRS["fitting"])
        for m in self.relevant_metrics():
            for g in METRIC_GROUPS.get(m, []):
                out |= METRIC_ATTRS[g]
        if self.propulsion in ("mwd", "ab"):
            out |= METRIC_ATTRS["nav"]
        return out

    def meta_ids(self, which: list[str]) -> set:
        out = set()
        for m in which:
            if m not in META_GROUPS:
                raise ValueError(f"unknown meta group {m!r}; choose from {', '.join(META_GROUPS)}")
            out |= META_GROUPS[m]
        return out


def _profile(d) -> dict[str, float]:
    if isinstance(d, str):
        d = {"preset": d}
    if "preset" in d:
        name = d["preset"].lower()
        if name not in DAMAGE_PROFILES:
            raise ValueError(f"unknown damage profile {name!r}; choose from {', '.join(DAMAGE_PROFILES)}")
        em, th, ki, ex = DAMAGE_PROFILES[name]
        return {"em": em, "thermal": th, "kinetic": ki, "explosive": ex}
    return {k: float(d.get(k, 0)) for k in ("em", "thermal", "kinetic", "explosive")}


def _target(d: dict) -> Target:
    base = TARGET_PRESETS.get(str(d.get("preset", "cruiser")).lower())
    if base is None:
        raise ValueError(f"unknown target preset {d.get('preset')!r}; choose from {', '.join(TARGET_PRESETS)}")
    t = Target(base.name, base.signature, base.velocity, base.distance, base.transversal_share, dict(base.resonance))
    for k in ("signature", "velocity", "distance", "transversal_share"):
        if k in d:
            setattr(t, k, float(d[k]))
    if "resists" in d:  # resist fractions (0.3 = 30%) -> resonance
        for k, v in d["resists"].items():
            t.resonance[k] = 1.0 - float(v)
    return t


_KNOWN_TOP = {"ship", "ships", "ship_groups", "name", "objective", "target", "incoming", "constraints",
              "fitting", "search"}


def from_dict(d: dict) -> Spec:
    unknown = set(d) - _KNOWN_TOP
    if unknown:
        raise ValueError(f"unknown spec sections: {', '.join(sorted(unknown))}")
    sp = Spec(name=d.get("name", ""))
    if "ship" in d:
        sp.ships = [d["ship"]]
    sp.ships += list(d.get("ships", []))
    sp.ship_groups = list(d.get("ship_groups", []))

    obj = d.get("objective", {})
    if "weights" in obj:
        sp.weights = {k: float(v) for k, v in obj["weights"].items()}
    elif "maximize" in obj:
        sp.weights = {obj["maximize"]: 1.0}
    elif "minimize" in obj:
        sp.weights = {obj["minimize"]: 1.0}
    sp.scales = {k: float(v) for k, v in obj.get("scales", {}).items()}
    for m in list(sp.weights) + list(sp.scales):
        if m not in METRICS:
            raise ValueError(f"unknown metric {m!r}; choose from {', '.join(METRICS)}")

    if "target" in d:
        sp.target = _target(d["target"])
    if "incoming" in d:
        sp.incoming = _profile(d["incoming"])

    for k, v in d.get("constraints", {}).items():
        if k == "cap_stable":
            if v:
                sp.constraints.append(Constraint("cap_seconds", math.inf, "min"))
            continue
        if k == "budget":
            k = "max_price"
        kind, _, metric = k.partition("_")
        if kind not in ("min", "max") or metric not in METRICS:
            raise ValueError(f"bad constraint {k!r}: use min_<metric> / max_<metric> with a metric from "
                             f"{', '.join(METRICS)}, or cap_stable / budget")
        sp.constraints.append(Constraint(metric, float(v), kind))

    f = d.get("fitting", {})
    for k in ("meta", "ammo_meta", "require", "exclude", "ammo", "subsystems"):
        if k in f:
            setattr(sp, k, list(f[k]))
    for k in ("tank", "propulsion", "weapons"):
        if k in f:
            setattr(sp, k, str(f[k]).lower())
    for k in ("weapon",):
        if k in f:
            setattr(sp, k, f[k])
    if "drones" in f:
        sp.drones = bool(f["drones"])
    sp.oversized_prop = bool(f.get("oversized_prop", False))
    sp.smartbombs = bool(f.get("smartbombs", False))
    sp.fill_slots = bool(f.get("fill_slots", True))
    sp.cpu_margin = float(f.get("cpu_margin", 0))
    sp.pg_margin = float(f.get("pg_margin", 0))
    sp.meta_ids(sp.meta)
    sp.meta_ids(sp.ammo_meta)
    if sp.tank not in ("shield", "armor", "hull", "any"):
        raise ValueError("fitting.tank must be shield, armor, hull or any")
    if sp.propulsion not in ("mwd", "ab", "none", "any"):
        raise ValueError("fitting.propulsion must be mwd, ab, none or any")
    if sp.weapons not in ("turret", "missile", "drone", "none", "any"):
        raise ValueError("fitting.weapons must be turret, missile, drone, none or any")

    s = d.get("search", {})
    sp.effort = s.get("effort", sp.effort)
    sp.seed = int(s.get("seed", sp.seed))
    if sp.effort not in ("quick", "normal", "thorough"):
        raise ValueError("search.effort must be quick, normal or thorough")
    return sp


def load(path: str | Path) -> Spec:
    with open(path, "rb") as f:
        return from_dict(tomllib.load(f))
