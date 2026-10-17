"""Derived ship statistics from a dogma calculation.

Conventions follow the game's fitting window and Pyfa: DPS excludes reload unless
asked, EHP uses a damage profile (uniform by default), sustained tank is the
repair rate the capacitor can keep up after non-tank modules take their share.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from evesim import capacitor
from evesim.dogma import const as C
from evesim.dogma.engine import Character, FitCalc, Item
from evesim.fit import Fit, check, is_launcher, is_turret, resources

UNIFORM = {"em": 0.25, "thermal": 0.25, "kinetic": 0.25, "explosive": 0.25}
SKILL_MISSILE_LAUNCHER_OPERATION, SKILL_DEFENDER = 3319, 3323


def normalize_profile(p: dict[str, float] | None) -> dict[str, float]:
    if not p:
        return dict(UNIFORM)
    tot = sum(max(p.get(k, 0.0), 0.0) for k in C.DAMAGE_TYPES)
    if tot <= 0:
        return dict(UNIFORM)
    return {k: max(p.get(k, 0.0), 0.0) / tot for k in C.DAMAGE_TYPES}


@dataclass
class Weapon:
    name: str
    kind: str                 # turret, missile, smartbomb, drone, disintegrator, vorton
    count: int
    volley: dict[str, float]  # per weapon (per drone for drones), by damage type
    cycle_s: float
    charge: str | None = None
    optimal: float = 0.0
    falloff: float = 0.0
    tracking: float = 0.0
    optimal_sig: float = 0.0
    explosion_radius: float = 0.0
    explosion_velocity: float = 0.0
    drf: float = 0.0
    shots_before_reload: int = 0
    reload_s: float = 0.0
    spool_max: float = 0.0    # extra damage multiplier at full spool (disintegrators)
    velocity: float = 0.0     # drone speed

    @property
    def volley_total(self) -> float:
        return sum(self.volley.values()) * self.count

    def dps(self, reload: bool = False, spooled: bool = False) -> float:
        if self.cycle_s <= 0:
            return 0.0
        cycle = self.cycle_s
        if reload and self.shots_before_reload and self.reload_s:
            cycle = (self.shots_before_reload * self.cycle_s + self.reload_s) / self.shots_before_reload
        mult = 1.0 + (self.spool_max if spooled else 0.0)
        return self.volley_total * mult / cycle

    def dps_by_type(self) -> dict[str, float]:
        if self.cycle_s <= 0:
            return {k: 0.0 for k in C.DAMAGE_TYPES}
        return {k: v * self.count / self.cycle_s for k, v in self.volley.items()}

    def range_summary(self) -> str:
        if self.kind == "missile":
            return f"{self.optimal / 1000:.1f} km flight"
        if self.kind == "drone":
            return "drone"
        return f"{self.optimal / 1000:.1f} + {self.falloff / 1000:.1f} km"


@dataclass
class Stats:
    fit: Fit
    calc: FitCalc
    problems: list[str]
    resources: dict[str, tuple[float, float]]
    slots: dict[str, tuple[int, int]]
    hardpoints: dict[str, tuple[int, int]]
    weapons: list[Weapon]
    hp: dict[str, float]
    resonance: dict[str, dict[str, float]]
    profile: dict[str, float]
    ehp: dict[str, float]
    cap: capacitor.CapResult
    cap_capacity: float
    cap_recharge_s: float
    tank_peak: dict[str, float]
    tank_sustained: dict[str, float]
    nav: dict[str, float]
    targeting: dict[str, float]
    drones: dict[str, float]
    price: float | None = None
    extras: dict = field(default_factory=dict)
    tank_sustained_reload: dict[str, float] = field(default_factory=dict)
    utility: dict = field(default_factory=dict)  # cargo, mining yield, remote repair

    @property
    def valid(self) -> bool:
        return not self.problems

    @property
    def dps(self) -> float:
        return sum(w.dps() for w in self.weapons)

    @property
    def weapon_dps(self) -> float:
        return sum(w.dps() for w in self.weapons if w.kind not in ("drone", "fighter"))

    @property
    def fighter_dps(self) -> float:
        return sum(w.dps() for w in self.weapons if w.kind == "fighter")

    @property
    def drone_dps(self) -> float:
        return sum(w.dps() for w in self.weapons if w.kind == "drone")

    @property
    def volley(self) -> float:
        return sum(w.volley_total for w in self.weapons)

    @property
    def ehp_total(self) -> float:
        return sum(self.ehp.values())

    def layer_ehp_factor(self, layer: str) -> float:
        return 1.0 / sum(self.profile[d] * self.resonance[layer][d] for d in C.DAMAGE_TYPES)

    @property
    def sustained_ehps(self) -> float:
        t = self.tank_sustained
        return (t["shield"] + t["passive_shield"]) * self.layer_ehp_factor("shield") + \
            t["armor"] * self.layer_ehp_factor("armor") + t["hull"] * self.layer_ehp_factor("hull")

    @property
    def sustained_ehps_reload(self) -> float:
        """Like sustained_ehps, with ancillary repairer reloads counted."""
        t = self.tank_sustained_reload or self.tank_sustained
        return (t["shield"] + t["passive_shield"]) * self.layer_ehp_factor("shield") + \
            t["armor"] * self.layer_ehp_factor("armor") + t["hull"] * self.layer_ehp_factor("hull")

    @property
    def peak_ehps(self) -> float:
        t = self.tank_peak
        return (t["shield"] + t["passive_shield"]) * self.layer_ehp_factor("shield") + \
            t["armor"] * self.layer_ehp_factor("armor") + t["hull"] * self.layer_ehp_factor("hull")

    def to_dict(self) -> dict:
        return {
            "ship": self.fit.ship.name,
            "valid": self.valid,
            "problems": self.problems,
            "fitting": {k: {"used": u, "total": t} for k, (u, t) in self.resources.items()},
            "slots": {k: {"used": u, "total": t} for k, (u, t) in self.slots.items()},
            "hardpoints": {k: {"used": u, "total": t} for k, (u, t) in self.hardpoints.items()},
            "offense": {
                "dps": self.dps, "weapon_dps": self.weapon_dps, "drone_dps": self.drone_dps,
                "fighter_dps": self.fighter_dps,
                "dps_with_reload": sum(w.dps(reload=True) for w in self.weapons),
                "volley": self.volley,
                "weapons": [{
                    "name": w.name, "kind": w.kind, "count": w.count, "charge": w.charge,
                    "dps": w.dps(), "volley": w.volley_total, "cycle_s": w.cycle_s,
                    "optimal": w.optimal, "falloff": w.falloff, "tracking": w.tracking,
                    "explosion_radius": w.explosion_radius, "explosion_velocity": w.explosion_velocity,
                } for w in self.weapons],
            },
            "defense": {
                "hp": self.hp, "resonance": self.resonance, "profile": self.profile,
                "ehp": self.ehp, "ehp_total": self.ehp_total,
                "tank_peak": self.tank_peak, "tank_sustained": self.tank_sustained,
                "peak_ehps": self.peak_ehps, "sustained_ehps": self.sustained_ehps,
                "sustained_ehps_with_reload": self.sustained_ehps_reload,
            },
            "capacitor": {
                "capacity": self.cap_capacity, "recharge_s": self.cap_recharge_s,
                "stable": self.cap.stable, "lasts_s": self.cap.lasts_s,
                "stable_fraction": self.cap.stable_fraction,
                "peak_recharge": self.cap.peak_recharge, "use_per_s": self.cap.use_per_s,
                "inject_per_s": self.cap.inject_per_s,
            },
            "navigation": self.nav,
            "targeting": self.targeting,
            "drones": self.drones,
            "utility": self.utility,
            "price": self.price,
        }


def _dmg(calc: FitCalc, item: Item, mult: float = 1.0) -> dict[str, float]:
    return {k: calc.get(item, a) * mult for k, a in zip(C.DAMAGE_TYPES, C.DAMAGE_ATTRS)}


def _cycle_s(calc: FitCalc, m: Item) -> float:
    eff = calc.data.effects.get(m.type.default_effect) if m.type.default_effect else None
    dur_attr = eff.duration_attr if eff and eff.duration_attr else None
    if dur_attr is None:
        for e in m.type.effects:
            if e.duration_attr:
                dur_attr = e.duration_attr
                break
    if dur_attr is None:
        return 0.0
    return (calc.get(m, dur_attr) + calc.get(m, "moduleReactivationDelay")) / 1000.0


def _shots(calc: FitCalc, m: Item) -> int:
    if not m.charge:
        return 0
    vol = m.charge.type.attrs.get(C.A_VOLUME, 0.0)
    cap = calc.get(m, C.A_CAPACITY)
    per_cycle = max(calc.get(m, "chargeRate"), 1.0)
    return int(cap / vol / per_cycle) if vol > 0 else 0


def _weapons(calc: FitCalc, fit: Fit) -> list[Weapon]:
    groups: dict[tuple, Weapon] = {}
    for spec, m in zip(fit.modules, calc.modules):
        if m.state < C.STATE_ACTIVE:
            continue
        de = m.type.default_effect
        t = m.type
        w = None
        if de in (C.E_PROJECTILE_FIRED, C.E_TARGET_ATTACK) and is_turret(t):
            if not m.charge and not any(calc.get(m, a) for a in C.DAMAGE_ATTRS):
                continue
            src = m.charge or m
            w = Weapon(t.name, "turret", 1, _dmg(calc, src, calc.get(m, "damageMultiplier")), _cycle_s(calc, m),
                       charge=m.charge.type.name if m.charge else None,
                       optimal=calc.get(m, "maxRange"), falloff=calc.get(m, "falloff"),
                       tracking=calc.get(m, "trackingSpeed"), optimal_sig=calc.get(m, "optimalSigRadius"))
        elif de == C.E_DISINTEGRATOR_ATTACK and m.charge:
            w = Weapon(t.name, "disintegrator", 1, _dmg(calc, m.charge, calc.get(m, "damageMultiplier")),
                       _cycle_s(calc, m), charge=m.charge.type.name,
                       optimal=calc.get(m, "maxRange"), falloff=calc.get(m, "falloff"),
                       tracking=calc.get(m, "trackingSpeed"), optimal_sig=calc.get(m, "optimalSigRadius"),
                       spool_max=calc.get(m, "damageMultiplierBonusMax"))
        elif de == C.E_VORTON and m.charge:
            w = Weapon(t.name, "vorton", 1, _dmg(calc, m.charge, calc.get(m, "damageMultiplier")),
                       _cycle_s(calc, m), charge=m.charge.type.name, optimal=calc.get(m, "maxRange"),
                       explosion_radius=calc.get(m.charge, "aoeCloudSize"),
                       explosion_velocity=calc.get(m.charge, "aoeVelocity"),
                       drf=calc.get(m.charge, "aoeDamageReductionFactor"))
        elif de == C.E_USE_MISSILES and m.charge and is_launcher(t):
            ch = m.charge
            if not ch.req & {SKILL_MISSILE_LAUNCHER_OPERATION, SKILL_DEFENDER}:
                continue
            mult = calc.char_attr("missileDamageMultiplier")
            w = Weapon(t.name, "missile", 1, _dmg(calc, ch, mult), _cycle_s(calc, m), charge=ch.type.name,
                       optimal=calc.get(ch, "maxVelocity") * calc.get(ch, "explosionDelay") / 1000.0,
                       explosion_radius=calc.get(ch, "aoeCloudSize"),
                       explosion_velocity=calc.get(ch, "aoeVelocity"),
                       drf=calc.get(ch, "aoeDamageReductionFactor"))
        elif de == C.E_SMARTBOMB:
            w = Weapon(t.name, "smartbomb", 1, _dmg(calc, m), _cycle_s(calc, m),
                       optimal=calc.get(m, "empFieldRange"))
        if w is None or sum(w.volley.values()) <= 0:
            continue
        w.shots_before_reload = _shots(calc, m)
        w.reload_s = calc.get(m, "reloadTime") / 1000.0
        key = (w.name, w.charge, round(sum(w.volley.values()), 6), round(w.cycle_s, 6))
        if key in groups:
            groups[key].count += 1
        else:
            groups[key] = w
    out = list(groups.values())
    for d in calc.drones:
        dmg = _dmg(calc, d, calc.get(d, "damageMultiplier"))
        if sum(dmg.values()) <= 0:
            continue
        out.append(Weapon(d.type.name, "drone", d.amount, dmg, calc.get(d, "speed") / 1000.0,
                          optimal=calc.get(d, "maxRange"), falloff=calc.get(d, "falloff"),
                          tracking=calc.get(d, "trackingSpeed"), optimal_sig=calc.get(d, "optimalSigRadius"),
                          velocity=calc.get(d, "maxVelocity")))
    out += _fighter_weapons(calc)
    return out


# Fighter abilities that deal damage: effect id -> (attribute prefix, uses limited charges).
FIGHTER_ATTACKS = {6465: ("fighterAbilityAttackMissile", False),   # primary attack
                   6431: ("fighterAbilityMissiles", True),          # missile salvo
                   6485: ("fighterAbilityLaunchBomb", True)}        # heavy fighter bomb
_FIGHTER_ATTACK_M = 6465
_FIGHTER_NO_AUTOSTART = {6441, 6443}  # microwarpdrive, evasive maneuvers
# Charges carried per squadron and rearm time per charge, by squadron role (as Pyfa).
_FIGHTER_SHOTS = {1: 0, 2: 12, 4: 6, 5: 3}
_FIGHTER_REARM_MS = {1: 0, 2: 4000, 4: 6000, 5: 20000}


def fighter_active_abilities(t) -> list[int]:
    """Abilities a squadron uses by default, as Pyfa: the standard attack, plus abilities
    listed before it (e.g. light fighters' missile salvo); bombs and propulsion stay off."""
    active, attack_seen = [], False
    for eid in sorted(t.effect_ids):
        if eid == _FIGHTER_ATTACK_M:
            active.append(eid)
            attack_seen = True
        elif not attack_seen and eid not in _FIGHTER_NO_AUTOSTART:
            active.append(eid)
    return active


def _fighter_weapons(calc: FitCalc) -> list[Weapon]:
    groups: dict[tuple, Weapon] = {}
    data = calc.data

    def opt(item, name: str) -> float:
        """Attribute value, or 0 when this ability has no such attribute."""
        return calc.get(item, name) if name in data._attr_by_name else 0.0

    for f in calc.fighters:
        role = int(calc.get(f, "fighterSquadronRole"))
        for eid in fighter_active_abilities(f.type):
            spec = FIGHTER_ATTACKS.get(eid)
            if not spec:
                continue
            prefix, has_charges = spec
            if prefix == "fighterAbilityLaunchBomb":
                bomb = data.types.get(int(calc.get(f, "fighterAbilityLaunchBombType")))
                if bomb is None:
                    continue
                dmg = {k: bomb.attr(a) * f.amount for k, a in zip(C.DAMAGE_TYPES, C.DAMAGE_ATTRS)}
            else:
                mult = opt(f, prefix + "DamageMultiplier") * f.amount
                dmg = {k: opt(f, prefix + "Damage" + suffix) * mult
                       for k, suffix in zip(C.DAMAGE_TYPES, ("EM", "Therm", "Kin", "Exp"))}
            cycle = opt(f, prefix + "Duration") / 1000.0
            if sum(dmg.values()) <= 0 or cycle <= 0:
                continue
            factor = opt(f, prefix + "DamageReductionFactor") or opt(f, prefix + "ReductionFactor")
            sens = opt(f, prefix + "DamageReductionSensitivity") or opt(f, prefix + "ReductionSensitivity")
            drf = math.log(factor) / math.log(sens) if factor > 0 and sens > 1 else 1.0
            label = "attack" if eid == _FIGHTER_ATTACK_M else ("missiles" if eid == 6431 else "bomb")
            w = Weapon(f"{f.type.name} ({label})", "fighter", 1, dmg, cycle,
                       optimal=opt(f, prefix + "RangeOptimal") or opt(f, prefix + "Range"),
                       falloff=opt(f, prefix + "RangeFalloff"),
                       explosion_radius=opt(f, prefix + "ExplosionRadius"),
                       explosion_velocity=opt(f, prefix + "ExplosionVelocity"), drf=drf,
                       velocity=calc.get(f, "maxVelocity"))
            if has_charges:
                w.shots_before_reload = _FIGHTER_SHOTS.get(role, 0)
                w.reload_s = (calc.get(f, "fighterRefuelingTime")
                              + _FIGHTER_REARM_MS.get(role, 0) * w.shots_before_reload) / 1000.0
            key = (w.name, round(sum(dmg.values()), 6), round(cycle, 6))
            if key in groups:
                groups[key].count += 1
            else:
                groups[key] = w
    return list(groups.values())


def _sustained(reps, capres, peak, with_reload: bool) -> dict[str, float]:
    """Repair rate the capacitor can keep up: what is left after non-repair modules is spent on
    the repairers with the most HP per GJ first. ``with_reload`` also spreads ancillary
    repairers' charge reloads over their cycles."""
    out = dict(peak)
    for layer in ("shield", "armor", "hull"):
        out[layer] = sum(amt / cyc * (rf if with_reload else 1.0) for ly, amt, cyc, _, rf in reps if ly == layer)
    rep_cap = sum(cap / cyc for _, _, cyc, cap, _ in reps)
    if rep_cap <= 0:
        return out
    available = capres.peak_recharge + capres.inject_per_s - (capres.use_per_s - rep_cap)
    if available >= rep_cap:
        return out
    out.update({"shield": 0.0, "armor": 0.0, "hull": 0.0})
    budget = max(available, 0.0)
    for layer, amt, cyc, cap, rf in sorted(reps, key=lambda r: -(r[1] / r[3]) if r[3] else -math.inf):
        rate = amt / cyc * (rf if with_reload else 1.0)
        need = cap / cyc
        if need <= 0:
            out[layer] += rate
            continue
        frac = min(1.0, budget / need)
        out[layer] += rate * frac
        budget -= need * frac
    return out


def _reps(calc: FitCalc):
    """Yield (layer, hp_per_cycle, cycle_s, cap_per_cycle, reload_factor) for active local
    repairers; reload_factor is the share of time an ancillary repairer spends cycling."""
    for m in calc.modules:
        if m.state < C.STATE_ACTIVE:
            continue
        de = m.type.default_effect
        cyc = _cycle_s(calc, m)
        if cyc <= 0:
            continue
        cap = calc.get(m, "capacitorNeed")
        rf = 1.0
        if m.charge and de in (C.E_FUELED_SHIELD_BOOSTING, C.E_FUELED_ARMOR_REPAIR):
            shots, reload = _shots(calc, m), calc.get(m, "reloadTime") / 1000.0
            if shots:
                rf = shots * cyc / (shots * cyc + reload)
        if de == C.E_SHIELD_BOOSTING:
            yield "shield", calc.get(m, "shieldBonus"), cyc, cap, rf
        elif de == C.E_FUELED_SHIELD_BOOSTING:
            yield "shield", calc.get(m, "shieldBonus"), cyc, (0.0 if m.charge else cap), rf
        elif de == C.E_ARMOR_REPAIR:
            yield "armor", calc.get(m, "armorDamageAmount"), cyc, cap, rf
        elif de == C.E_FUELED_ARMOR_REPAIR:
            amt = calc.get(m, "armorDamageAmount")
            if m.charge:
                amt *= calc.get(m, "chargedArmorDamageMultiplier")
            yield "armor", amt, cyc, cap, rf
        elif de == C.E_STRUCTURE_REPAIR:
            yield "hull", calc.get(m, "structureDamageAmount"), cyc, cap, rf


def _cap_users(calc: FitCalc, assume_nos: bool = False) -> list[capacitor.CapUser]:
    users = []
    for m in calc.modules:
        if m.state < C.STATE_ACTIVE:
            continue
        de = m.type.default_effect
        cyc = _cycle_s(calc, m)
        if cyc <= 0:
            continue
        if de == C.E_POWER_BOOSTER:
            if m.charge:
                users.append(capacitor.CapUser(m.type.name, -calc.get(m.charge, "capacitorBonus"), cyc,
                                               _shots(calc, m), calc.get(m, "reloadTime") / 1000.0))
            continue
        if de == C.E_FUELED_SHIELD_BOOSTING and m.charge:
            continue
        if de == C.E_NOSFERATU:
            if assume_nos:
                users.append(capacitor.CapUser(m.type.name, -calc.get(m, "powerTransferAmount"), cyc))
            continue
        need = calc.get(m, "capacitorNeed")
        if need > 0:
            users.append(capacitor.CapUser(m.type.name, need, cyc, stagger=not is_turret(m.type)))
    return users


def compute(fit: Fit, char: Character, profile: dict[str, float] | None = None,
            calc: FitCalc | None = None, simulate_cap: bool = True, assume_nos: bool = False) -> Stats:
    calc = calc or fit.calc(char)
    ship = calc.ship
    g = calc.get
    prof = normalize_profile(profile)
    problems = check(fit, char, calc)
    res = resources(calc)

    slots = {s: (len(fit.by_slot(s)), int(g(ship, a))) for s, a in
             (("high", C.A_HI_SLOTS), ("med", C.A_MED_SLOTS), ("low", C.A_LOW_SLOTS), ("rig", C.A_RIG_SLOTS))}
    hardpoints = {"turret": (sum(1 for m in fit.modules if is_turret(m.type)), int(g(ship, C.A_TURRET_SLOTS))),
                  "launcher": (sum(1 for m in fit.modules if is_launcher(m.type)), int(g(ship, C.A_LAUNCHER_SLOTS)))}

    hp = {"shield": g(ship, "shieldCapacity"), "armor": g(ship, "armorHP"), "hull": g(ship, "hp")}
    resonance = {
        "shield": dict(zip(C.DAMAGE_TYPES, (g(ship, a) for a in C.SHIELD_RESONANCES))),
        "armor": dict(zip(C.DAMAGE_TYPES, (g(ship, a) for a in C.ARMOR_RESONANCES))),
        "hull": dict(zip(C.DAMAGE_TYPES, (g(ship, a) for a in C.HULL_RESONANCES))),
    }
    ehp = {layer: hp[layer] / max(sum(prof[d] * resonance[layer][d] for d in C.DAMAGE_TYPES), 1e-9)
           for layer in hp}

    cap_capacity = g(ship, "capacitorCapacity")
    cap_recharge = g(ship, "rechargeRate")
    users = _cap_users(calc, assume_nos)
    if simulate_cap:
        capres = capacitor.simulate(cap_capacity, cap_recharge, users)
    else:
        capres = capacitor.simulate(cap_capacity, cap_recharge, users, max_time_s=0)

    # Tank: peak and capacitor-limited sustained repair (HP/s per layer).
    reps = list(_reps(calc))
    peak = {"shield": 0.0, "armor": 0.0, "hull": 0.0}
    for layer, amt, cyc, _, _ in reps:
        peak[layer] += amt / cyc
    passive = 2.5 * hp["shield"] / (g(ship, "shieldRechargeRate") / 1000.0) if hp["shield"] else 0.0
    peak["passive_shield"] = passive
    sustained = _sustained(reps, capres, peak, with_reload=False)
    sustained_reload = _sustained(reps, capres, peak, with_reload=True)
    mass = g(ship, C.A_MASS)
    agility = g(ship, "agility")
    nav = {
        "max_velocity": g(ship, C.A_MAX_VELOCITY),
        "align_time": math.log(4) * agility * mass / 1e6,
        "warp_speed": g(ship, "baseWarpSpeed") * g(ship, "warpSpeedMultiplier"),
        "signature_radius": g(ship, C.A_SIGNATURE_RADIUS),
        "mass": mass,
        "agility": agility,
    }
    targeting = {
        "lock_range": g(ship, "maxTargetRange"),
        "scan_resolution": g(ship, "scanResolution"),
        "max_targets": min(g(ship, "maxLockedTargets"), calc.char_attr("maxLockedTargets")),
        "sensor_strength": max(g(ship, a) for a in ("scanRadarStrength", "scanLadarStrength",
                                                    "scanMagnetometricStrength", "scanGravimetricStrength")),
    }
    drones = {
        "bandwidth_used": sum(d.active * (d.type.attr("droneBandwidthUsed") or 0) for d in fit.drones),
        "bandwidth": g(ship, "droneBandwidth"),
        "bay_used": sum(d.count * d.type.attrs.get(C.A_VOLUME, 0) for d in fit.drones),
        "bay": g(ship, "droneCapacity"),
        "max_active": calc.char_attr("maxActiveDrones"),
        "control_range": calc.char_attr("droneControlDistance"),
        "fighter_tubes_used": sum(1 for f in fit.fighters if f.active),
        "fighter_tubes": g(ship, "fighterTubes"),
        "fighter_bay_used": sum(f.amount * f.type.attrs.get(C.A_VOLUME, 0) for f in fit.fighters),
        "fighter_bay": g(ship, "fighterCapacity"),
    }
    utility = {
        "cargo": g(ship, C.A_CAPACITY),
        "mining_yield": _mining_yield(calc),
        "remote_repair": _remote_reps(calc),
        "ewar": _ewar(calc),
    }
    return Stats(fit, calc, problems, res, slots, hardpoints, _weapons(calc, fit), hp, resonance, prof, ehp,
                 capres, cap_capacity, cap_recharge / 1000.0, peak, sustained, nav, targeting, drones,
                 tank_sustained_reload=sustained_reload, utility=utility)


# Remote repair effects: effect id -> (layer, amount attribute)
_REMOTE_REPAIR = {
    6186: ("shield", "shieldBonus"), 6652: ("shield", "shieldBonus"),
    6188: ("armor", "armorDamageAmount"), 6651: ("armor", "armorDamageAmount"), 7166: ("armor", "armorDamageAmount"),
    6185: ("hull", "structureDamageAmount"),
    6688: ("shield", "shieldBonus"), 6687: ("armor", "armorDamageAmount"), 6689: ("hull", "structureDamageAmount"),
}
_MINING_EFFECTS = {67, 17}  # miningLaser (modules), mining (drones)

# Electronic warfare on a target: default effect -> (kind, strength attribute, unit).
# Strength is per module as your skills and hull make it; stacking on the target is not modelled.
EWAR_EFFECTS = {
    6187: ("neut", "energyNeutralizerAmount", "GJ"),
    6197: ("nos", "powerTransferAmount", "GJ"),
    6426: ("web", "speedFactor", "%"),
    6425: ("paint", "signatureRadiusBonus", "%"),
    6470: ("ecm", None, "jam strength"),
    6422: ("damp", "maxTargetRangeBonus", "%"),
    6424: ("tracking_disruptor", "trackingSpeedBonus", "%"),
    6423: ("guidance_disruptor", "aoeVelocityBonus", "%"),
}
_ECM_ATTRS = ("scanGravimetricStrengthBonus", "scanLadarStrengthBonus", "scanMagnetometricStrengthBonus",
              "scanRadarStrengthBonus")


def _mining_yield(calc: FitCalc) -> float:
    """Ore/ice mined per second (m3/s), including critical-success bonus yield, like Pyfa."""
    total = 0.0
    for it in [m for m in calc.modules if m.state >= C.STATE_ACTIVE] + calc.drones:
        if it.type.default_effect not in _MINING_EFFECTS:
            continue
        cyc = _cycle_s(calc, it)
        amount = calc.get(it, "miningAmount")
        if cyc <= 0 or not amount:
            continue
        crit = calc.get(it, "miningCritChance") * calc.get(it, "miningCritBonusYield")
        total += amount / cyc * (1 + crit) * it.amount
    return total


def _ewar(calc: FitCalc) -> list[dict]:
    """What each active EWAR/tackle module does to a target: strength, range, and per-second
    capacitor drain for neutralizers and nosferatu."""
    out: dict[tuple, dict] = {}
    for m in calc.modules:
        if m.state < C.STATE_ACTIVE:
            continue
        spec = EWAR_EFFECTS.get(m.type.default_effect)
        scram = calc.get(m, "warpScrambleStrength") if not spec else 0.0
        if not spec and scram <= 0:
            continue
        if spec:
            kind, attr, unit = spec
            strength = max(calc.get(m, a) for a in _ECM_ATTRS) if kind == "ecm" else abs(calc.get(m, attr))
        else:
            kind, strength, unit = ("scram" if scram >= 2 else "point"), scram, "points"
        cyc = _cycle_s(calc, m)
        entry = {"kind": kind, "name": m.type.name, "count": 1, "strength": strength, "unit": unit,
                 "optimal": calc.get(m, "maxRange"), "falloff": calc.get(m, "falloffEffectiveness"),
                 "cycle_s": cyc, "per_second": strength / cyc if kind in ("neut", "nos") and cyc else 0.0}
        key = (m.type.id, round(strength, 6), round(entry["optimal"], 3))
        if key in out:
            out[key]["count"] += 1
            out[key]["per_second"] += entry["per_second"]
        else:
            out[key] = entry
    return list(out.values())


def _remote_reps(calc: FitCalc) -> dict[str, float]:
    """HP/s your remote repairers and logistics drones give to others, per layer."""
    out = {"shield": 0.0, "armor": 0.0, "hull": 0.0}
    for it in [m for m in calc.modules if m.state >= C.STATE_ACTIVE] + calc.drones:
        spec = _REMOTE_REPAIR.get(it.type.default_effect)
        if not spec:
            continue
        cyc = _cycle_s(calc, it)
        if cyc <= 0:
            continue
        amount = calc.get(it, spec[1])
        if it.charge is not None and it.type.default_effect == 6651:
            amount *= calc.get(it, "chargedArmorDamageMultiplier")
        out[spec[0]] += amount / cyc * it.amount
    return out
