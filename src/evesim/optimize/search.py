"""Skill-constrained fit optimization.

The search is a heuristic over an exact simulator: every candidate fit is scored
with the full dogma engine, so stacking penalties, skill bonuses, hull bonuses
and fitting limits are always right; what is not guaranteed is global optimality.

Pipeline per ship:

1. **Candidates.** Modules the character can use, allowed by the spec (meta
   groups, tank/propulsion choices, exclusions, budget), that can go on the hull.
   Each is evaluated alone on the hull with weapons; modules that improve no
   relevant metric are dropped, and within a module group only Pareto-optimal
   variants (benefit vs. CPU/PG/calibration/price) survive.
2. **Weapons.** Each turret/launcher type is evaluated with each allowed ammo on
   the bare hull; the best few configurations go forward.
3. **Search.** For each weapon configuration: greedy fill of mid/low/rig slots,
   then first-improvement local search over single-slot swaps, then iterated
   local search with random perturbations. Drones and ammo are re-picked at the
   end.

Score = sum(weight * metric / scale) - penalty * (relative constraint violation),
so feasible fits always beat infeasible ones and the shortfall is graded.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field

from evesim.data.store import CAT_DRONE, CAT_FIGHTER, CAT_MODULE, StaticData, Type
from evesim.dogma import const as C
from evesim.dogma.engine import Character
from evesim.fit import (FIGHTER_TUBE_ATTRS, DroneSpec, FighterSpec, Fit, ModuleSpec, charge_fits, default_state,
                        fighter_class, is_launcher, is_turret, module_fits_ship, ship_modes, slot_of)
from evesim.market import Prices
from evesim.optimize.spec import DAMAGE_METRICS, METRIC_ATTRS, METRICS, Spec
from evesim.stats import _MINING_EFFECTS, _REMOTE_REPAIR, EWAR_EFFECTS, Stats, compute

PENALTY = 1000.0
CACHE_LIMIT = 20_000  # evaluations kept for re-use; cleared when full and between hulls
GROUP_PROPULSION = 46

# Metric scales: a weight of 1 means "one scale unit of this metric is worth one point".
DEFAULT_SCALES = {
    "dps": 100.0, "weapon_dps": 100.0, "drone_dps": 100.0, "fighter_dps": 100.0, "applied_dps": 100.0, "volley": 1000.0,
    "ehp": 10_000.0, "shield_ehp": 10_000.0, "armor_ehp": 10_000.0,
    "sustained_tank": 100.0, "peak_tank": 100.0, "speed": 1000.0, "align_time": 10.0, "signature": 100.0,
    "lock_range": 50_000.0, "scan_resolution": 500.0, "weapon_range": 10_000.0, "cap_seconds": 300.0,
    "cap_stable_fraction": 1.0, "cap_delta": 10.0, "price": 100_000_000.0, "cpu_left": 100.0, "pg_left": 100.0,
    "hull_ehp": 10_000.0, "survival_time": 300.0, "dps_per_misk": 10.0, "ehp_per_misk": 1000.0, "cargo": 1000.0,
    "warp_speed": 5.0, "mass": 10_000_000.0, "max_targets": 5.0, "sensor_strength": 20.0, "lock_time": 5.0,
    "drone_control_range": 50_000.0, "cap_capacity": 1000.0, "cap_recharge": 10.0, "mining_yield": 10.0,
    "remote_repair": 100.0, "neut_rate": 10.0, "tackle_range": 10_000.0, "web_range": 10_000.0,
    "web_strength": 50.0, "paint_strength": 25.0, "ecm_strength": 5.0, "damp_strength": 25.0,
    "disruption_strength": 25.0,
}
EFFORT = {  # (weapon configs, perturbation rounds, perturbation size)
    "screen": (1, 0, 0),  # greedy fill only: used to rank many hulls before searching the best ones
    "quick": (1, 1, 2),
    "normal": (3, 3, 3),
    "thorough": (6, 10, 3),
}
GROUP_SMARTBOMB = 72
GROUPS_STRUCTURE_FIGHTERS = {4777, 4778, 4779}  # launched from citadels, not ships

# Ship classes for ship_classes = [...]: names of in-game ship groups.
SHIP_CLASSES = {
    "frigate": ["Frigate", "Assault Frigate", "Interceptor", "Covert Ops", "Electronic Attack Ship",
                "Stealth Bomber", "Logistics Frigate", "Expedition Frigate", "Prototype Exploration Ship"],
    "destroyer": ["Destroyer", "Interdictor", "Tactical Destroyer", "Command Destroyer"],
    "cruiser": ["Cruiser", "Heavy Assault Cruiser", "Heavy Interdiction Cruiser", "Force Recon Ship",
                "Combat Recon Ship", "Logistics", "Strategic Cruiser", "Flag Cruiser"],
    "battlecruiser": ["Combat Battlecruiser", "Attack Battlecruiser", "Command Ship"],
    "battleship": ["Battleship", "Black Ops", "Marauder"],
    "capital": ["Carrier", "Dreadnought", "Lancer Dreadnought", "Force Auxiliary", "Supercarrier", "Titan",
                "Command Carrier", "Capital Industrial Ship"],
    "industrial": ["Hauler", "Deep Space Transport", "Blockade Runner", "Freighter", "Jump Freighter",
                   "Industrial Command Ship", "Expedition Command Ship"],
    "mining": ["Mining Barge", "Exhumer", "Expedition Frigate", "Industrial Command Ship", "Capital Industrial Ship"],
    "corvette": ["Corvette", "Shuttle"],
}

_SHIELD_ATTRS = {"shieldCapacity", "shieldEmDamageResonance", "shieldThermalDamageResonance",
                 "shieldKineticDamageResonance", "shieldExplosiveDamageResonance", "shieldRechargeRate"}
_ARMOR_ATTRS = {"armorHP", "armorEmDamageResonance", "armorThermalDamageResonance",
                "armorKineticDamageResonance", "armorExplosiveDamageResonance"}
_HULL_ATTRS = {"hp", "emDamageResonance", "thermalDamageResonance", "kineticDamageResonance",
               "explosiveDamageResonance"}


@dataclass
class Evaluation:
    fit: Fit
    stats: Stats
    score: float
    objective: float
    violation: float
    metrics: dict[str, float]
    problems: list[str]

    @property
    def feasible(self) -> bool:
        return self.violation <= 1e-9


@dataclass
class Result:
    ship: Type
    best: Evaluation | None
    alternatives: list[Evaluation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    evaluations: int = 0
    seconds: float = 0.0


@dataclass
class WeaponConfig:
    racks: list[tuple[Type, Type | None, int]]  # (weapon, ammo, count)

    def label(self) -> str:
        if not self.racks:
            return "no weapons"
        return " + ".join(f"{n}x {w.name}" + (f" ({a.name})" if a else "") for w, a, n in self.racks)


class Optimizer:
    def __init__(self, data: StaticData, char: Character, spec: Spec, prices: Prices | None = None,
                 log=lambda msg: None):
        self.data = data
        self.char = char
        self.spec = spec
        self.prices = prices or Prices(data)
        self.log = log
        self.rng = random.Random(spec.seed)
        self.scales = dict(DEFAULT_SCALES)
        self.scales.update(spec.scales)
        self.relevant = spec.relevant_metrics()
        self.metric_names = self.relevant | {"dps", "ehp", "price", "speed", "cap_delta"}
        fitting = {"cpu", "power", "upgradeCost", "cpuOutput", "powerOutput", "upgradeCapacity"}
        self.relevant_attr_ids = {data.attr_id(a) for a in spec.relevant_attrs() - fitting
                                  if a in data._attr_by_name}
        self._fitting_out = {data.attr_id(a) for a in ("cpuOutput", "powerOutput", "upgradeCapacity")}
        self._fitting_use = {data.attr_id(a) for a in ("cpu", "power", "upgradeCost")}
        self._ewar_attr_ids = {data.attr_id(a) for g in ("neut", "tackle", "web", "paint", "ecm", "damp", "disrupt")
                               for a in METRIC_ATTRS[g] if a in data._attr_by_name}
        self._cache: dict[tuple, Evaluation] = {}
        self.evaluations = 0
        self.max_price = min((c.bound for c in spec.constraints if c.metric == "price" and c.kind == "max"),
                             default=math.inf)
        self.allowed_meta = spec.meta_ids(spec.meta)
        self.allowed_ammo_meta = spec.meta_ids(spec.ammo_meta)
        self.exclude = {data.type_by_name(n).id for n in spec.exclude}
        self._touch_cache: dict[int, set[int]] = {}
        self._cost: dict[int, tuple[float, float, float]] = {}  # measured cpu/pg/calibration per module
        self._sim_default = False  # search on capacitor averages; finalists are re-checked with the sim
        self.screened: list[tuple[str, float]] = []  # (hull, screening score) when many hulls were ranked

    # ---- evaluation -------------------------------------------------------------------------
    def _key(self, fit: Fit) -> tuple:
        return (fit.ship.id,
                tuple(sorted((m.type.id, m.charge.id if m.charge else 0, m.state) for m in fit.modules)),
                tuple(sorted((d.type.id, d.count, d.active) for d in fit.drones)),
                tuple(sorted((x.type.id, x.amount) for x in fit.fighters)))

    def evaluate(self, fit: Fit, sim_cap: bool | None = None) -> Evaluation:
        key = self._key(fit)
        sim = self._sim_default if sim_cap is None else sim_cap
        hit = self._cache.get(key)
        if hit is not None and (hit.stats.extras.get("sim") or not sim):
            return hit
        st = compute(fit, self.char, profile=self.spec.incoming, simulate_cap=sim)
        st.extras["sim"] = sim
        if not sim:
            # Without the time simulation, judge stability from the averages.
            st.cap.stable = st.cap.delta_per_s >= 0
            st.cap.lasts_s = None if st.cap.stable else _estimate_lasts(st)
        st.price = self.prices.fit_price(fit)
        st.calc = None  # the dogma state is large; nothing downstream needs it once stats exist
        self.evaluations += 1
        metrics = {m: METRICS[m].fn(st, self.spec) for m in self.metric_names}

        viol = 0.0
        for c in self.spec.constraints:
            v = metrics[c.metric]
            if c.bound == math.inf:  # cap stable
                viol += 0.0 if v == math.inf else 1.0 + max(0.0, 1.0 - min(v, 600.0) / 600.0)
            else:
                viol += c.shortfall(v)
        for kind, margin in (("cpu", self.spec.cpu_margin), ("power", self.spec.pg_margin), ("calibration", 0.0)):
            used, out = st.resources[kind]
            if used + margin > out + 1e-6:
                viol += (used + margin - out) / max(out, 1.0) + 0.05
        other = [p for p in st.problems if not p.startswith(("cpu ", "power ", "calibration "))]
        viol += len(other)
        if self.spec.propulsion in ("mwd", "ab"):
            want = C.E_MWD if self.spec.propulsion == "mwd" else C.E_AB
            if not any(want in m.type.effect_ids for m in fit.modules):
                viol += 1.0

        obj = 0.0
        for m, w in self.spec.weights.items():
            v = metrics[m]
            if v == math.inf:
                v = 3600.0
            sign = 1.0 if METRICS[m].higher_is_better else -1.0
            obj += w * sign * v / self.scales.get(m, 1.0)
        # Tiny tie-breaker: among equal fits prefer spare CPU/PG and lower price, which lets the
        # search walk off plateaus (e.g. swap an oversized module for one that leaves room).
        cpu_used, cpu_out = st.resources["cpu"]
        pg_used, pg_out = st.resources["power"]
        frugal = 1e-3 * ((cpu_out - cpu_used) / max(cpu_out, 1.0) + (pg_out - pg_used) / max(pg_out, 1.0)) \
            - 1e-4 * (st.price or 0.0) / 1e8
        ev = Evaluation(fit, st, obj + frugal - PENALTY * viol, obj, viol, metrics, st.problems)
        if len(self._cache) >= CACHE_LIMIT:
            self._cache.clear()
        self._cache[key] = ev
        return ev

    # ---- candidates -------------------------------------------------------------------------
    def _touched(self, t: Type) -> set[int]:
        """Attributes a module can change on the fit (its modifiers plus hard-coded effects)."""
        hit = self._touch_cache.get(t.id)
        if hit is not None:
            return hit
        a = self.data.attr_id
        out: set[int] = set()
        for e in t.effects:
            if e.category == C.EC_TARGET and e.id not in (C.E_PROJECTILE_FIRED, C.E_TARGET_ATTACK):
                continue
            for m in e.modifiers:
                if m.modified is not None:
                    out.add(m.modified)
        eids = set(t.effect_ids)
        if eids & {C.E_MWD, C.E_AB}:
            out |= {C.A_MAX_VELOCITY, C.A_SIGNATURE_RADIUS, C.A_MASS}
        if eids & {C.E_SHIELD_BOOSTING, C.E_FUELED_SHIELD_BOOSTING}:
            out |= {a("shieldBonus"), a("shieldCapacity")}
        if eids & {C.E_ARMOR_REPAIR, C.E_FUELED_ARMOR_REPAIR}:
            out |= {a("armorDamageAmount"), a("armorHP")}
        if C.E_STRUCTURE_REPAIR in eids:
            out |= {a("structureDamageAmount"), a("hp")}
        if C.E_POWER_BOOSTER in eids:
            out |= {a("capacitorBonus"), a("capacitorCapacity")}
        if C.E_ADAPTIVE_ARMOR_HARDENER in eids:
            out |= set(C.ARMOR_RESONANCES)
        if t.default_effect in _REMOTE_REPAIR:
            out.add(a(_REMOTE_REPAIR[t.default_effect][1]))
        if t.default_effect in _MINING_EFFECTS:
            out.add(a("miningAmount"))
        if t.default_effect in EWAR_EFFECTS or t.attrs.get(a("warpScrambleStrength")):
            out |= {x for x in self._ewar_attr_ids if x in t.attrs}
        self._touch_cache[t.id] = out
        return out

    def _helps_fitting(self, t: Type) -> bool:
        """True for fitting modules: more CPU/PG/calibration, or cheaper modules."""
        for e in t.effects:
            for m in e.modifiers:
                if m.modified in self._fitting_out and m.op in (C.OP_MOD_ADD, C.OP_POST_PERCENT, C.OP_POST_MUL,
                                                                C.OP_PRE_MUL):
                    v = t.attr(m.modifying) if m.modifying else 0.0
                    if (m.op == C.OP_MOD_ADD and v > 0) or (m.op == C.OP_POST_PERCENT and v > 0) or \
                            (m.op in (C.OP_POST_MUL, C.OP_PRE_MUL) and v > 1):
                        return True
                if m.modified in self._fitting_use and m.op == C.OP_POST_PERCENT:
                    v = t.attr(m.modifying) if m.modifying else 0.0
                    if v < 0:
                        return True
        return False

    def _layer_ok(self, t: Type) -> bool:
        if self.spec.tank == "any":
            return True
        names = {self.data.attributes[x].name for x in self._touched(t) if x in self.data.attributes}
        layers = {k for k, s in (("shield", _SHIELD_ATTRS), ("armor", _ARMOR_ATTRS), ("hull", _HULL_ATTRS))
                  if names & s}
        if not layers or self.spec.tank in layers:
            return True
        return False

    def _usable(self, t: Type, ship: Type, meta: set) -> bool:
        if t.id in self.exclude:
            return False
        if t.market_group is None:
            return False
        if t.meta_group not in meta:
            return False
        if not self.char.meets(t):
            return False
        if self.prices(t.id) > self.max_price:
            return False
        return module_fits_ship(t, ship, self.data)

    def module_pools(self, ship: Type, base: Fit) -> dict[str, list[Type]]:
        """Candidate modules for mid/low/rig/high-utility slots, pruned."""
        raw: dict[str, list[Type]] = {"med": [], "low": [], "rig": [], "high": []}
        for t in self.data.types.values():
            if t.category_id != CAT_MODULE:
                continue
            slot = slot_of(t)
            if slot not in raw or _is_weapon(t):
                continue
            if not self._usable(t, ship, self.allowed_meta) or not self._layer_ok(t):
                continue
            if t.group_id == GROUP_SMARTBOMB and not self.spec.smartbombs:
                continue
            if t.group_id == GROUP_PROPULSION:
                kind = "mwd" if C.E_MWD in t.effect_ids else "ab" if C.E_AB in t.effect_ids else "other"
                if self.spec.propulsion == "none" or kind == "other":
                    continue
                if self.spec.propulsion in ("mwd", "ab") and kind != self.spec.propulsion:
                    continue
            if not (self._touched(t) & self.relevant_attr_ids) and not self._helps_fitting(t):
                continue
            raw[slot].append(t)

        dims = sorted(self.relevant | ({"cap_delta"} if self.relevant & {"sustained_tank", "cap_seconds",
                                                                         "cap_stable_fraction"} else set()))
        base_ev = self.evaluate(base, sim_cap=False)
        cpu_out = base_ev.stats.resources["cpu"][1]
        pg_out = base_ev.stats.resources["power"][1]
        mass = base.ship.attr(C.A_MASS)

        def plausible(t: Type) -> bool:
            # Drop modules no fitting of this hull could power (e.g. capital modules on a cruiser)
            # and propulsion sized for another hull class.
            if t.attr(C.A_POWER) > pg_out * 1.5 or t.attr(C.A_CPU) > cpu_out * 1.5:
                return False
            if t.group_id == GROUP_PROPULSION and not self.spec.oversized_prop:
                return 0.1 <= t.attr(C.A_MASS_ADDITION) / max(mass, 1.0) <= 1.5
            return True

        def gains(probe: Fit, probe_ev: Evaluation, t: Type) -> tuple[list[float], Evaluation]:
            f = probe.copy()
            f.modules.append(ModuleSpec(t, default_state(t, self.data), self._default_charge(t)))
            ev = self.evaluate(f, sim_cap=False)
            out = []
            for m in dims:
                v, b = ev.metrics.get(m, 0.0), probe_ev.metrics.get(m, 0.0)
                if v == math.inf or b == math.inf:
                    out.append(0.0 if v == b else (1.0 if v == math.inf else -1.0))
                else:
                    out.append((v - b) if METRICS[m].higher_is_better else (b - v))
            return out, ev

        raw = {k: [t for t in v if plausible(t)] for k, v in raw.items()}
        first: dict[int, tuple[list[float], Evaluation]] = {}
        for types in raw.values():
            for t in types:
                first[t.id] = gains(base, base_ev, t)

        # Second probe with "enablers" fitted, so modules that only work in combination
        # (boost amplifiers with a booster, prop rigs with a prop mod ...) are not lost.
        enabler_effects = [(C.E_SHIELD_BOOSTING, C.E_FUELED_SHIELD_BOOSTING), (C.E_ARMOR_REPAIR, C.E_FUELED_ARMOR_REPAIR),
                           (C.E_POWER_BOOSTER,), (C.E_MWD,), (C.E_AB,), tuple(_MINING_EFFECTS),
                           tuple(_REMOTE_REPAIR)]
        probe2 = base.copy()
        for effs in enabler_effects:
            cands = [t for types in raw.values() for t in types if set(t.effect_ids) & set(effs)]
            if cands:
                t = max(cands, key=lambda t: (sum(g > 1e-9 for g in first[t.id][0]), t.meta_group == 2))
                probe2.modules.append(ModuleSpec(t, default_state(t, self.data), self._default_charge(t)))
        probe2_ev = self.evaluate(probe2, sim_cap=False)

        pools = {}
        for slot, types in raw.items():
            vecs = []
            for t in types:
                g1, ev = first[t.id]
                g2, _ = gains(probe2, probe2_ev, t)
                required_prop = t.group_id == GROUP_PROPULSION and self.spec.propulsion in ("mwd", "ab")
                if not (any(g > 1e-9 for g in g1) or any(g > 1e-9 for g in g2) or self._helps_fitting(t)
                        or required_prop):
                    continue
                res = ev.stats.resources
                cost = (res["cpu"][0] - base_ev.stats.resources["cpu"][0],
                        res["power"][0] - base_ev.stats.resources["power"][0],
                        res["calibration"][0] - base_ev.stats.resources["calibration"][0],
                        self.prices(t.id))
                if not self._helps_fitting(t):
                    self._cost[t.id] = cost[:3]
                vecs.append((t, g1 + g2, cost, 0.0))
            pools[slot] = [v[0] for v in _pareto_by_family([(t, g, c) for t, g, c, _ in vecs])]
        return pools

    def _default_charge(self, t: Type) -> Type | None:
        if C.E_POWER_BOOSTER in t.effect_ids or C.E_FUELED_ARMOR_REPAIR in t.effect_ids:
            ch = [c for c in self._charges(t) if c is not None]
            if ch:
                # biggest cap booster that fits; nanite paste for AARs
                return max(ch, key=lambda c: (c.attr("capacitorBonus") or 0, -c.attrs.get(C.A_VOLUME, 0)))
        return None

    def _charges(self, t: Type) -> list[Type]:
        out = []
        groups = {int(t.attrs[a]) for a in (604, 605, 606, 609, 610) if t.attrs.get(a)}
        if not groups:
            return [None]
        for c in self.data.types.values():
            if c.group_id in groups and c.market_group is not None and charge_fits(t, c) and self.char.meets(c) \
                    and c.meta_group in self.allowed_ammo_meta:
                if self.spec.ammo and c.name not in self.spec.ammo:
                    continue
                out.append(c)
        return out or [None]

    # ---- weapons ----------------------------------------------------------------------------
    def weapon_configs(self, ship: Type, base: Fit) -> list[WeaponConfig]:
        calc = base.calc(self.char)
        hp_t = int(calc.get(calc.ship, C.A_TURRET_SLOTS))
        hp_l = int(calc.get(calc.ship, C.A_LAUNCHER_SLOTS))
        n_high = int(calc.get(calc.ship, C.A_HI_SLOTS)) - sum(1 for m in base.modules if m.slot == "high")
        if self.spec.weapons in ("drone", "none") or not (self.relevant & DAMAGE_METRICS):
            # No damage metric in play (mining, logistics, hauling ...): keep the high slots free.
            return [WeaponConfig([])]
        weapons = []
        for t in self.data.types.values():
            if t.category_id != CAT_MODULE or not (is_turret(t) or is_launcher(t)):
                continue
            if self.spec.weapon and t.name.lower() != self.spec.weapon.lower():
                continue
            if self.spec.weapons == "turret" and not is_turret(t):
                continue
            if self.spec.weapons == "missile" and not is_launcher(t):
                continue
            if not self._usable(t, ship, self.allowed_meta):
                continue
            weapons.append(t)

        pg_out = calc.get(calc.ship, C.A_POWER_OUTPUT)

        def rack(w, ammo, n) -> Evaluation:
            f = base.copy()
            for _ in range(n):
                f.modules.append(ModuleSpec(w, default_state(w, self.data), ammo))
            return self.evaluate(f, sim_cap=False)

        def rank(ev: Evaluation) -> float:
            # Ignore constraint penalties here: they depend on the rest of the fit.
            return ev.objective - PENALTY * _fitting_violation(ev)

        # 1) Rank weapon types with one probe charge (the hardest-hitting allowed one).
        first = []
        for w in weapons:
            n = min(hp_t if is_turret(w) else hp_l, n_high)
            if n <= 0 or n * w.attr(C.A_POWER) > 1.5 * pg_out:
                continue
            charges = self._charges(w)
            probes = {max(charges, key=_charge_damage), max(charges, key=_charge_reach)}
            evs = [rack(w, p, n) for p in probes]
            ev = max(evs, key=rank)
            if ev.metrics["dps"] <= 0:
                continue
            first.append((w, n, charges, ev))
        fam: dict[int, list] = {}
        for row in first:
            fam.setdefault(row[0].variation_parent or row[0].id, []).append(row)
        survivors = []
        for rows in fam.values():
            for r in rows:
                rc = r[3].stats.resources
                dominated = any(
                    rank(o[3]) >= rank(r[3]) and o[3].stats.resources["power"][0] <= rc["power"][0]
                    and o[3].stats.resources["cpu"][0] <= rc["cpu"][0] and self.prices(o[0].id) <= self.prices(r[0].id)
                    and (rank(o[3]), -self.prices(o[0].id)) != (rank(r[3]), -self.prices(r[0].id))
                    for o in rows if o is not r)
                if not dominated:
                    survivors.append(r)
        survivors.sort(key=lambda r: -rank(r[3]))
        top = survivors[:max(3 * EFFORT[self.spec.effort][0], 6)]

        # 2) Try every allowed charge on the leading weapons.
        scored = []
        for w, n, charges, _ in top:
            for ammo in charges:
                ev = rack(w, ammo, n)
                if ev.metrics["dps"] <= 0:
                    continue
                scored.append((rank(ev), ev.metrics["dps"], w, ammo, n,
                               ev.stats.resources["power"][0], ev.stats.resources["cpu"][0]))
        if not scored:
            return [WeaponConfig([])]
        k = EFFORT[self.spec.effort][0]
        picked: list[tuple] = []
        seen_w = set()
        for row in sorted(scored, key=lambda r: (-r[0], -r[1])):  # best ammo per weapon type
            if row[2].id in seen_w:
                continue
            seen_w.add(row[2].id)
            picked.append(row)
        configs = [WeaponConfig([(r[2], r[3], r[4])]) for r in picked[:k]]
        # Also try the cheapest-to-fit option among decent weapons, which frees grid for tank.
        decent = [r for r in picked if r[1] >= 0.6 * picked[0][1]]
        if decent and k > 1:
            cheap = min(decent, key=lambda r: (r[5], r[6]))
            cfg = WeaponConfig([(cheap[2], cheap[3], cheap[4])])
            if cfg.label() not in {c.label() for c in configs}:
                configs.append(cfg)
        # Mixed hardpoints: add the best weapon of the other kind to leftover high slots.
        for cfg in list(configs):
            w, ammo, n = cfg.racks[0]
            left = n_high - n
            other_hp = hp_l if is_turret(w) else hp_t
            m = min(left, other_hp)
            if m > 0 and self.spec.weapons == "any":
                other = next((r for r in picked if is_turret(r[2]) != is_turret(w)), None)
                if other:
                    configs.append(WeaponConfig(cfg.racks + [(other[2], other[3], m)]))
        return configs

    def _apply_weapons(self, base: Fit, cfg: WeaponConfig) -> Fit:
        f = base.copy()
        for w, ammo, n in cfg.racks:
            for _ in range(n):
                f.modules.append(ModuleSpec(w, default_state(w, self.data), ammo))
        return f

    # ---- drones -----------------------------------------------------------------------------
    def drone_candidates(self, ship: Type) -> list[Type]:
        if not self.spec.drones or not ship.attr("droneBandwidth") or not ship.attr("droneCapacity"):
            return []
        out = []
        for t in self.data.types.values():
            if t.category_id != CAT_DRONE or t.market_group is None:
                continue
            if not any(t.attrs.get(a) for a in C.DAMAGE_ATTRS):
                continue
            if t.meta_group not in self.allowed_meta or not self.char.meets(t):
                continue
            if t.id in self.exclude or self.prices(t.id) > self.max_price:
                continue
            out.append(t)
        return out

    def pick_drones(self, fit: Fit, candidates: list[Type]) -> Fit:
        """Best drone flight, and for carriers the best fighter squadrons."""
        return self.pick_fighters(self._pick_drone_flight(fit, candidates))

    def fighter_candidates(self, ship: Type) -> list[Type]:
        if not self.spec.drones or not ship.attr("fighterTubes"):
            return []
        out = []
        for t in self.data.types.values():
            if t.category_id != CAT_FIGHTER or t.market_group is None or t.group_id in GROUPS_STRUCTURE_FIGHTERS:
                continue
            if t.meta_group not in self.allowed_meta or not self.char.meets(t) or t.id in self.exclude:
                continue
            if self.prices(t.id) > self.max_price:
                continue
            out.append(t)
        return out

    def pick_fighters(self, fit: Fit) -> Fit:
        """Fill launch tubes with the squadrons that add the most to the objective's damage."""
        cands = self.fighter_candidates(fit.ship)
        if not cands or not (self.relevant & DAMAGE_METRICS):
            return fit
        f = fit.copy()
        f.fighters = []
        probe = f.copy()
        probe.fighters = [FighterSpec(t, 1) for t in cands]
        calc = probe.calc(self.char)
        sizes = {it.type.id: max(int(calc.get(it, "fighterSquadronMaxSize")), 1) for it in calc.fighters}
        probe.fighters = [FighterSpec(t, sizes[t.id]) for t in cands]
        st = compute(probe, self.char, profile=self.spec.incoming, simulate_cap=False)
        from evesim.application import weapon_factor
        use_applied = "applied_dps" in self.relevant
        value: dict[str, float] = {}
        for w in st.weapons:
            if w.kind != "fighter":
                continue
            name = w.name.rsplit(" (", 1)[0]
            v = w.dps() * (weapon_factor(w, self.spec.target) if use_applied else 1.0)
            value[name] = value.get(name, 0.0) + v
        c0 = f.calc(self.char)
        tubes = int(c0.get(c0.ship, "fighterTubes"))
        free = {role: int(c0.get(c0.ship, attr)) for role, attr in FIGHTER_TUBE_ATTRS.items()}
        bay = c0.get(c0.ship, "fighterCapacity")
        ranked = sorted((t for t in cands if value.get(t.name, 0) > 0),
                        key=lambda t: (-value[t.name], self.prices(t.id)))
        while len(f.fighters) < tubes:
            pick = None
            for t in ranked:
                role = fighter_class(t)
                vol = sizes[t.id] * t.attrs.get(C.A_VOLUME, 0.0)
                if free.get(role, 0) > 0 and vol <= bay + 1e-6:
                    pick = (t, role, vol)
                    break
            if pick is None:
                break
            t, role, vol = pick
            f.fighters.append(FighterSpec(t, sizes[t.id]))
            free[role] -= 1
            bay -= vol
        return f

    def _pick_drone_flight(self, fit: Fit, candidates: list[Type]) -> Fit:
        if not candidates:
            return fit
        f = fit.copy()
        f.drones = []
        calc = f.calc(self.char)
        bw = calc.get(calc.ship, "droneBandwidth")
        bay = calc.get(calc.ship, "droneCapacity")
        max_active = int(calc.char_attr("maxActiveDrones"))
        # Value each drone type once in this fit (bonuses from modules/hull included).
        probe = fit.copy()
        probe.drones = [DroneSpec(t, 1, 1) for t in candidates]
        st = compute(probe, self.char, profile=self.spec.incoming, simulate_cap=False)
        use_applied = "applied_dps" in self.relevant
        from evesim.application import weapon_factor
        values = {}
        for w in st.weapons:
            if w.kind != "drone":
                continue
            t = next(c for c in candidates if c.name == w.name)
            v = w.dps()
            if use_applied:
                v *= weapon_factor(w, self.spec.target)
            values[t.id] = (v, t)
        opts = []
        for tid, (v, t) in values.items():
            opts.append((v, t, t.attr("droneBandwidthUsed") or 0.0, t.attrs.get(C.A_VOLUME, 0.0)))
        # Keep the best type per (bandwidth, volume) class so the DP stays tiny.
        best_by_size: dict[tuple, tuple] = {}
        for o in opts:
            k = (o[2], o[3])
            if k not in best_by_size or (o[0], -self.prices(o[1].id)) > (best_by_size[k][0], -self.prices(best_by_size[k][1].id)):
                best_by_size[k] = o
        opts = sorted(best_by_size.values(), key=lambda o: -o[0])
        best = (0.0, [])

        def rec(i, n_left, bw_left, bay_left, chosen, val):
            nonlocal best
            if val > best[0]:
                best = (val, list(chosen))
            if i >= len(opts) or n_left == 0:
                return
            v, t, b, vol = opts[i]
            maxn = n_left
            if b:
                maxn = min(maxn, int(bw_left // b))
            if vol:
                maxn = min(maxn, int(bay_left // vol))
            for n in range(maxn, -1, -1):
                if n:
                    chosen.append((t, n))
                rec(i + 1, n_left - n, bw_left - n * b, bay_left - n * vol, chosen, val + n * v)
                if n:
                    chosen.pop()

        rec(0, max_active, bw, bay, [], 0.0)
        f.drones = [DroneSpec(t, n, n) for t, n in best[1]]
        return f

    # ---- search -----------------------------------------------------------------------------
    def _affordable(self, current: Evaluation, old: Type | None, new: Type | None) -> bool:
        """Cheap pre-check: would swapping ``old`` for ``new`` obviously overflow CPU/PG/calibration?"""
        if new is None:
            return True
        cn = self._cost.get(new.id)
        if cn is None:
            return True  # fitting modules and unmeasured types are always tried
        co = self._cost.get(old.id, (0.0, 0.0, 0.0)) if old is not None else (0.0, 0.0, 0.0)
        res = current.stats.resources
        margins = (self.spec.cpu_margin, self.spec.pg_margin, 0.0)
        for i, kind in enumerate(("cpu", "power", "calibration")):
            used, out = res[kind]
            if used + margins[i] > out + 1e-6:
                return True  # already over: let the search explore its way back
            if used + cn[i] - co[i] + margins[i] > out + 1e-6:
                return False
        return True

    def _assemble(self, base: Fit, slots: dict[str, list[Type]]) -> Fit:
        f = base.copy()
        for slot in ("high", "med", "low", "rig", "prop", "mode"):
            for t in slots.get(slot, ()):
                f.modules.append(ModuleSpec(t, default_state(t, self.data), self._default_charge(t)))
        return f

    def _local_search(self, base: Fit, slots: dict[str, list[Type]], free: dict[str, int],
                      pools: dict[str, list[Type]], current: Evaluation) -> tuple[dict, Evaluation]:
        """Coordinate descent: for each position (a fitted module or an empty slot), try every
        alternative from that slot's pool and keep the best; repeat until a pass changes nothing."""
        improved = True
        while improved:
            improved = False
            positions = []
            for slot, pool in pools.items():
                if free.get(slot, 0) <= 0 or not pool:
                    continue
                present = list(dict.fromkeys(slots.get(slot, [])))  # distinct, in order
                if len(slots.get(slot, [])) < free[slot]:
                    present.append(None)  # an empty position
                positions += [(slot, old) for old in present]
            self.rng.shuffle(positions)
            for slot, old in positions:
                if old is not None and old not in slots[slot]:
                    continue  # changed earlier in this pass
                options = [t for t in pools[slot] if t is not old]
                if old is not None and slot not in ("prop", "mode"):
                    options.append(None)  # removal
                best = None
                for new in options:
                    if not self._affordable(current, old, new):
                        continue
                    trial = {k: list(v) for k, v in slots.items()}
                    if old is not None:
                        trial[slot].remove(old)
                    if new is not None:
                        trial[slot].append(new)
                    ev = self.evaluate(self._assemble(base, trial))
                    if ev.score > (best[0].score if best else current.score) + 1e-9:
                        best = (ev, trial)
                if best:
                    current, slots, improved = best[0], best[1], True
        return slots, current

    def _greedy(self, base: Fit, slots, free, pools, current):
        while True:
            best = None
            for slot, pool in pools.items():
                if len(slots[slot]) >= free.get(slot, 0):
                    continue
                for new in pool:
                    if not self._affordable(current, None, new):
                        continue
                    trial = {k: list(v) for k, v in slots.items()}
                    trial[slot].append(new)
                    ev = self.evaluate(self._assemble(base, trial))
                    if best is None or ev.score > best[0].score:
                        best = (ev, trial)
            if best is None or best[0].score <= current.score + 1e-9:
                return slots, current
            current, slots = best[0], best[1]

    def optimize_config(self, base: Fit, cfg: WeaponConfig, drones: list[Type]) -> Evaluation:
        armed = self.pick_drones(self._apply_weapons(base, cfg), drones)
        pools = self.module_pools(base.ship, armed)
        if self.spec.effort != "screen":
            self.log(f"  {cfg.label()}: candidates " + " ".join(f"{k}={len(v)}" for k, v in pools.items()))
        calc = armed.calc(self.char)
        used = {s: sum(1 for m in armed.modules if m.slot == s) for s in ("high", "med", "low", "rig")}
        free = {"high": int(calc.get(calc.ship, C.A_HI_SLOTS)) - used["high"],
                "med": int(calc.get(calc.ship, C.A_MED_SLOTS)) - used["med"],
                "low": int(calc.get(calc.ship, C.A_LOW_SLOTS)) - used["low"],
                "rig": int(calc.get(calc.ship, C.A_RIG_SLOTS)) - used["rig"]}
        slots: dict[str, list[Type]] = {"high": [], "med": [], "low": [], "rig": []}
        if self.spec.propulsion in ("mwd", "ab"):
            # Required propulsion gets a reserved mid slot; the search may only swap variants.
            props = [t for t in pools["med"] if t.group_id == GROUP_PROPULSION]
            pools = dict(pools, med=[t for t in pools["med"] if t.group_id != GROUP_PROPULSION], prop=props)
            if props and free["med"] > 0:
                free["med"] -= 1
                free["prop"] = 1
                slots["prop"] = [max(props, key=lambda t: self.evaluate(self._assemble(armed, {"prop": [t]})).score)]
            else:
                slots["prop"] = []
        modes = ship_modes(base.ship, self.data)
        if modes and not any(m.slot == "mode" for m in armed.modules):
            # T3 destroyers: the tactical mode is one more slot the search chooses.
            pools["mode"] = modes
            free["mode"] = 1
            slots["mode"] = [max(modes, key=lambda t: self.evaluate(self._assemble(armed, dict(slots, mode=[t]))).score)]
        current = self.evaluate(self._assemble(armed, slots))
        slots, current = self._greedy(armed, slots, free, pools, current)
        if self.spec.effort == "screen":
            return current
        slots, current = self._local_search(armed, slots, free, pools, current)

        rounds, size = EFFORT[self.spec.effort][1:]
        best_slots, best = slots, current
        for _ in range(rounds):
            trial = {k: list(v) for k, v in best_slots.items()}
            for _ in range(size):
                slot = self.rng.choice([s for s in pools if pools[s] and free.get(s, 0) > 0] or ["med"])
                if not pools.get(slot):
                    continue
                if trial[slot] and (len(trial[slot]) >= free[slot] or self.rng.random() < 0.5 or slot in ("prop", "mode")):
                    trial[slot].pop(self.rng.randrange(len(trial[slot])))
                if len(trial[slot]) < free[slot]:
                    trial[slot].append(self.rng.choice(pools[slot]))
            ev = self.evaluate(self._assemble(armed, trial))
            trial, ev = self._local_search(armed, trial, free, pools, ev)
            if ev.score > best.score + 1e-9:
                best_slots, best = trial, ev

        # Final touches: fill slots the objective left empty, re-pick drones and ammo.
        if self.spec.fill_slots:
            best_slots, best = self._fill_empty(armed, best_slots, free, pools, best)
        fit = self.pick_drones(best.fit, drones)
        best = self._repick_ammo(self.evaluate(fit))
        if not self.spec.cap_simulation_needed:
            return self.evaluate(best.fit, sim_cap=True)
        # Verify with the time-domain capacitor simulation; if the averages were too optimistic,
        # continue the local search with the simulation switched on.
        checked = self.evaluate(best.fit, sim_cap=True)
        if checked.violation > best.violation + 1e-9:
            self._sim_default = True
            try:
                slots = {k: list(v) for k, v in best_slots.items()}
                slots, checked = self._local_search(armed, slots, free, pools, checked)
            finally:
                self._sim_default = False
        return checked

    def _fill_empty(self, base: Fit, slots, free, pools, current: Evaluation):
        """Put something useful in slots the objective did not need: whatever adds the most
        EHP and DPS without lowering the objective or breaking a constraint."""
        def utility(ev: Evaluation) -> float:
            return ev.metrics["ehp"] / 10_000.0 + ev.metrics["dps"] / 100.0

        for slot in ("med", "low", "rig"):
            while len(slots.get(slot, [])) < free.get(slot, 0) and pools.get(slot):
                best = None
                for new in pools[slot]:
                    if not self._affordable(current, None, new):
                        continue
                    trial = {k: list(v) for k, v in slots.items()}
                    trial[slot].append(new)
                    ev = self.evaluate(self._assemble(base, trial))
                    if ev.violation > current.violation + 1e-9 or ev.objective < current.objective - 1e-9:
                        continue
                    if utility(ev) <= utility(current) + 1e-9:
                        continue
                    if best is None or utility(ev) > utility(best[0]):
                        best = (ev, trial)
                if best is None:
                    break
                current, slots = best
        return slots, current

    def _repick_ammo(self, ev: Evaluation) -> Evaluation:
        best = ev
        weapon_types = {m.type.id: m.type for m in ev.fit.modules if (is_turret(m.type) or is_launcher(m.type))}
        for wt in weapon_types.values():
            for ammo in self._charges(wt):
                f = best.fit.copy()
                for m in f.modules:
                    if m.type.id == wt.id:
                        m.charge = ammo
                cand = self.evaluate(f)
                if cand.score > best.score + 1e-9:
                    best = cand
        return best

    def base_fit(self, ship: Type, subsystems: list[Type] | None = None) -> Fit:
        f = Fit(ship, name=self.spec.name or "evesim optimized")
        if subsystems is None and ship.attrs.get(1367):
            subsystems = [self.data.type_by_name(n) for n in self.spec.subsystems if n != "auto"]
        for t in subsystems or []:
            f.modules.append(ModuleSpec(t, C.STATE_ONLINE))
        for name in self.spec.require:
            t = self.data.type_by_name(name)
            f.modules.append(ModuleSpec(t, default_state(t, self.data), self._default_charge(t)))
        return f

    def run_ship(self, ship: Type) -> Result:
        self._cache.clear()
        t0 = time.time()
        n0 = self.evaluations
        res = Result(ship, None)
        if not self.char.meets(ship):
            miss = ", ".join(f"{self.data.types[s].name} {lvl}" for s, lvl in self.char.missing(ship).items())
            res.notes.append(f"cannot fly {ship.name}: needs {miss}")
            return res
        subsystems = None
        if ship.attrs.get(1367):
            if not self.spec.subsystems:
                res.notes.append(f"{ship.name} is a T3 cruiser: list its four subsystems under fitting.subsystems, "
                                 f'or set subsystems = "auto" to search them')
                return res
            if self.spec.subsystems == ["auto"]:
                subsystems = self.pick_subsystems(ship)
                self.log(f"{ship.name}: subsystems " + ", ".join(t.name for t in subsystems))
        base = self.base_fit(ship, subsystems)
        configs = self.weapon_configs(ship, base)
        if self.spec.effort != "screen":
            self.log(f"{ship.name}: {len(configs)} weapon configuration(s): " + "; ".join(c.label() for c in configs))
        drones = self.drone_candidates(ship)
        evals = []
        for cfg in configs:
            ev = self.optimize_config(base, cfg, drones)
            if self.spec.effort != "screen":
                self.log(f"  {cfg.label()}: score {ev.score:.3f} ({'feasible' if ev.feasible else 'infeasible'})")
            evals.append(ev)
        evals.sort(key=lambda e: -e.score)
        res.best = evals[0]
        res.alternatives = evals[1:]
        res.evaluations = self.evaluations - n0
        res.seconds = time.time() - t0
        return res

    def pick_subsystems(self, ship: Type) -> list[Type]:
        """Choose a T3 cruiser's subsystems: screen every combination with a greedy fit."""
        by_slot: dict[int, list[Type]] = {}
        for t in self.data.types.values():
            if t.category_id == 32 and int(t.attrs.get(1380, 0)) == ship.id and t.market_group \
                    and self.char.meets(t):
                by_slot.setdefault(int(t.attrs.get(1366, 0)), []).append(t)
        if len(by_slot) < 4:
            raise ValueError(f"cannot fly {ship.name}: subsystem skills missing")
        slots = sorted(by_slot)
        drones = self.drone_candidates(ship)
        scores: dict[tuple, float] = {}

        def score(combo: tuple) -> float:
            if combo not in scores:
                base = self.base_fit(ship, list(combo))
                cfgs = self.weapon_configs(ship, base)
                scores[combo] = self.optimize_config(base, cfgs[0], drones).score
            return scores[combo]

        # Coordinate descent: change one subsystem at a time while it helps (about 10-25
        # screens instead of all 81 combinations).
        saved = self.spec.effort
        self.spec.effort = "screen"
        try:
            current = tuple(by_slot[s][0] for s in slots)
            improved = True
            while improved:
                improved = False
                for i, s in enumerate(slots):
                    for alt in by_slot[s]:
                        trial = current[:i] + (alt,) + current[i + 1:]
                        if score(trial) > score(current) + 1e-9:
                            current, improved = trial, True
        finally:
            self.spec.effort = saved
        return list(current)

    def ships(self) -> list[Type]:
        out = [self.data.type_by_name(n) for n in self.spec.ships if n != "all"]
        groups = {g.lower() for g in self.spec.ship_groups}
        for cls in self.spec.ship_classes:
            groups |= {g.lower() for g in SHIP_CLASSES[cls]}
        everything = "all" in self.spec.ships or "all" in self.spec.ship_classes
        if groups or everything:
            for t in sorted(self.data.types.values(), key=lambda t: t.name):
                if t.category_id == 6 and t.market_group is not None and self.char.meets(t) and t not in out \
                        and (everything or t.group_name.lower() in groups):
                    out.append(t)
        if not out:
            raise ValueError("no ship to optimize: set ship, ships, ship_classes or ship_groups "
                             "(and check the character can fly them)")
        return out

    def run(self) -> list[Result]:
        ships = self.ships()
        top = self.spec.screen_top
        if len(ships) > top:
            # Many hulls: rank them all with a fast greedy fit, then search the best few properly.
            self.log(f"screening {len(ships)} hulls, then searching the best {top}")
            saved = self.spec.effort
            self.spec.effort = "screen"
            screened = []
            try:
                for ship in ships:
                    r = self.run_ship(ship)
                    screened.append((r.best.score if r.best else -math.inf, ship, r))
                    if r.best:
                        self.log(f"  screen {ship.name}: {r.best.score:.3f}")
            finally:
                self.spec.effort = saved
            screened.sort(key=lambda x: -x[0])
            results = [self.run_ship(ship) for score, ship, _ in screened[:top] if score > -math.inf]
            self.screened = [(ship.name, score) for score, ship, _ in screened]
        else:
            results = [self.run_ship(s) for s in ships]
        results.sort(key=lambda r: -(r.best.score if r.best else -math.inf))
        return results


_WEAPON_EFFECTS = {C.E_PROJECTILE_FIRED, C.E_TARGET_ATTACK, C.E_USE_MISSILES, C.E_DISINTEGRATOR_ATTACK, C.E_VORTON}


def _is_weapon(t: Type) -> bool:
    """Turrets and launchers that shoot (mining lasers are turrets too, but not weapons)."""
    return (is_turret(t) or is_launcher(t)) and t.default_effect in _WEAPON_EFFECTS


def _charge_damage(c: Type | None) -> float:
    return sum(c.attrs.get(a, 0.0) for a in C.DAMAGE_ATTRS) if c else 0.0


def _charge_reach(c: Type | None) -> float:
    """Proxy for range: missile flight distance, or a turret charge's range multiplier."""
    if c is None:
        return 0.0
    flight = c.attr("maxVelocity", 0.0) * c.attr("explosionDelay", 0.0)
    return flight if flight else c.attr("weaponRangeMultiplier", 1.0)


def _fitting_violation(ev: Evaluation) -> float:
    v = 0.0
    for kind in ("cpu", "power"):
        used, out = ev.stats.resources[kind]
        if used > out:
            v += (used - out) / max(out, 1.0) + 0.05
    return v


def _estimate_lasts(st: Stats) -> float:
    net = st.cap.use_per_s - st.cap.inject_per_s - st.cap.peak_recharge * 0.75
    return st.cap_capacity / net if net > 0 else 3600.0


def _pareto_by_family(vecs):
    """Keep meta variants (T1/T2/named versions of one module) not dominated by a sibling.
    Different modules are never compared: their value depends on what else is fitted."""
    by_group: dict[int, list] = {}
    for v in vecs:
        by_group.setdefault(v[0].variation_parent or v[0].id, []).append(v)
    out = []
    for items in by_group.values():
        for i, (t, g, c) in enumerate(items):
            dominated = False
            for j, (t2, g2, c2) in enumerate(items):
                if i == j:
                    continue
                ge = all(a >= b - 1e-9 for a, b in zip(g2, g)) and all(a <= b + 1e-9 for a, b in zip(c2, c))
                strict = any(a > b + 1e-9 for a, b in zip(g2, g)) or any(a < b - 1e-9 for a, b in zip(c2, c))
                if ge and (strict or j < i):
                    dominated = True
                    break
            if not dominated:
                out.append((t, g, c))
    return out
