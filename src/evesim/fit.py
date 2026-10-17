"""Fit model, EFT import/export and fitting-rule checks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from evesim.data.store import CAT_DRONE, CAT_FIGHTER, CAT_MODULE, CAT_SHIP, CAT_SUBSYSTEM, StaticData, Type
from evesim.dogma import const as C
from evesim.dogma.engine import Character, FitCalc

SLOTS = ("low", "med", "high", "rig", "subsystem", "mode")
GROUP_SHIP_MODES = 1306
_SLOT_EFFECTS = {C.E_LO_POWER: "low", C.E_MED_POWER: "med", C.E_HI_POWER: "high",
                 C.E_RIG_SLOT: "rig", C.E_SUBSYSTEM: "subsystem"}
_SLOT_COUNT_ATTR = {"low": C.A_LOW_SLOTS, "med": C.A_MED_SLOTS, "high": C.A_HI_SLOTS, "rig": C.A_RIG_SLOTS}
CHARGE_GROUP_ATTRS = (604, 605, 606, 609, 610)
A_CHARGE_SIZE = 128
A_TECH_LEVEL = 422
A_MAX_GROUP_FITTED, A_MAX_GROUP_ACTIVE, A_MAX_TYPE_FITTED = 1544, 763, 2431
A_FITS_TO_SHIP_TYPE = 1380
A_MAX_SUBSYSTEMS = 1367
A_DRONE_BANDWIDTH, A_DRONE_CAPACITY, A_DRONE_BANDWIDTH_USED = 1271, 283, 1272
A_FIGHTER_IS_LIGHT, A_FIGHTER_IS_SUPPORT, A_FIGHTER_IS_HEAVY = 2212, 2213, 2214
FIGHTER_TUBE_ATTRS = {"light": "fighterLightSlots", "support": "fighterSupportSlots", "heavy": "fighterHeavySlots"}


def slot_of(t: Type) -> str | None:
    if t.group_id == GROUP_SHIP_MODES:
        return "mode"
    for e in t.effect_ids:
        s = _SLOT_EFFECTS.get(e)
        if s:
            return s
    return None


_modes_cache: dict[tuple[int, int], list[Type]] = {}


def ship_modes(ship: Type, data: StaticData) -> list[Type]:
    """Tactical modes of a T3 destroyer (empty for other hulls)."""
    key = (id(data), ship.id)
    if key not in _modes_cache:
        _modes_cache[key] = sorted((t for t in data.types.values() if t.group_id == GROUP_SHIP_MODES
                                    and t.name.startswith(ship.name + " ")), key=lambda t: t.name)
    return _modes_cache[key]


def can_activate(t: Type, data: StaticData) -> bool:
    for e in t.effect_ids:
        if e == C.E_ONLINE:
            continue
        eff = data.effects.get(e)
        if eff and eff.category in (C.EC_ACTIVE, C.EC_TARGET):
            return True
    return False


A_REACTIVATION_DELAY = 669


def default_state(t: Type, data: StaticData) -> int:
    """Active when the module can run continuously. Burst modules with a long reactivation
    delay (assault damage controls, cloaks, micro jump drives, bombs) stay online: their
    active bonus is up only a few seconds at a time, so it is not a sustained stat."""
    if slot_of(t) in ("rig", "subsystem", "mode"):
        return C.STATE_ONLINE
    if t.attrs.get(A_REACTIVATION_DELAY, 0.0) > 0:
        return C.STATE_ONLINE
    return C.STATE_ACTIVE if can_activate(t, data) else C.STATE_ONLINE


def is_turret(t: Type) -> bool:
    return C.E_TURRET_FITTED in t.effect_ids


def is_launcher(t: Type) -> bool:
    return C.E_LAUNCHER_FITTED in t.effect_ids


@dataclass
class ModuleSpec:
    type: Type
    state: int
    charge: Type | None = None

    @property
    def slot(self) -> str:
        return slot_of(self.type) or "high"


@dataclass
class DroneSpec:
    type: Type
    count: int
    active: int


@dataclass
class FighterSpec:
    """One fighter squadron in a launch tube."""
    type: Type
    amount: int  # fighters in the squadron (EFT's "x6")
    active: bool = True

    @property
    def role(self) -> str:
        return fighter_class(self.type)


def fighter_class(t: Type) -> str:
    if t.attrs.get(A_FIGHTER_IS_HEAVY):
        return "heavy"
    if t.attrs.get(A_FIGHTER_IS_SUPPORT):
        return "support"
    return "light"


@dataclass
class Fit:
    ship: Type
    modules: list[ModuleSpec] = field(default_factory=list)
    drones: list[DroneSpec] = field(default_factory=list)
    name: str = ""
    fighters: list[FighterSpec] = field(default_factory=list)

    def calc(self, char: Character) -> FitCalc:
        return FitCalc(char, self.ship,
                       [(m.type, m.state, m.slot, m.charge) for m in self.modules],
                       [(d.type, d.active) for d in self.drones if d.active > 0],
                       [(f.type, f.amount) for f in self.fighters if f.active and f.amount > 0])

    def by_slot(self, slot: str) -> list[ModuleSpec]:
        return [m for m in self.modules if m.slot == slot]

    def copy(self) -> "Fit":
        return Fit(self.ship, [ModuleSpec(m.type, m.state, m.charge) for m in self.modules],
                   [DroneSpec(d.type, d.count, d.active) for d in self.drones], self.name,
                   [FighterSpec(f.type, f.amount, f.active) for f in self.fighters])

    # ---- EFT ---------------------------------------------------------------------------
    def to_eft(self) -> str:
        lines = [f"[{self.ship.name}, {self.name or 'evesim fit'}]"]
        for slot in SLOTS:
            mods = self.by_slot(slot)
            if not mods and slot in ("subsystem", "mode"):
                continue
            for m in mods:
                line = m.type.name
                if m.charge is not None:
                    line += f", {m.charge.name}"
                if m.state == C.STATE_OFFLINE:
                    line += " /OFFLINE"
                lines.append(line)
            lines.append("")
        if self.drones:
            lines.append("")
            for d in self.drones:
                lines.append(f"{d.type.name} x{d.count}")
        if self.fighters:
            lines.append("")
            for f in self.fighters:
                lines.append(f"{f.type.name} x{f.amount}")
        return "\n".join(lines).rstrip() + "\n"

    @classmethod
    def from_eft(cls, text: str, data: StaticData) -> "Fit":
        lines = [ln.strip() for ln in text.strip().splitlines()]
        m = re.match(r"^\[(.+?),\s*(.*)\]$", lines[0])
        if not m:
            raise ValueError("EFT text must start with '[Ship, Fit name]'")
        ship = data.type_by_name(m.group(1))
        if ship.category_id != CAT_SHIP:
            raise ValueError(f"{ship.name} is not a ship")
        fit = cls(ship, name=m.group(2))
        for line in lines[1:]:
            if not line or line.startswith("[") and line.lower().startswith("[empty"):
                continue
            offline = False
            if line.upper().endswith("/OFFLINE"):
                line, offline = line[: -len("/OFFLINE")].strip(), True
            qty = re.match(r"^(.*\S)\s+x(\d+)$", line)
            if qty:
                t = data.type_by_name(qty.group(1))
                if t.category_id == CAT_DRONE:
                    n = int(qty.group(2))
                    fit.drones.append(DroneSpec(t, n, n))
                elif t.category_id == CAT_FIGHTER:
                    fit.fighters.append(FighterSpec(t, int(qty.group(2))))
                continue  # cargo is ignored
            name, _, charge = line.partition(",")
            t = data.type_by_name(name)
            if t.category_id not in (CAT_MODULE, CAT_SUBSYSTEM):
                continue
            ch = data.type_by_name(charge) if charge.strip() else None
            state = C.STATE_OFFLINE if offline else default_state(t, data)
            fit.modules.append(ModuleSpec(t, state, ch))
        cap_active_drones(fit)
        return fit


def cap_active_drones(fit: Fit, max_active: int = 5, bandwidth: float | None = None):
    """Mark drones active in listed order within the active-drone and bandwidth limits."""
    if bandwidth is None:
        bandwidth = fit.ship.attr(A_DRONE_BANDWIDTH)
    left_n, left_bw = max_active, bandwidth
    for d in fit.drones:
        bw = d.type.attr(A_DRONE_BANDWIDTH_USED) or 0.0
        n = min(d.count, left_n, int(left_bw // bw) if bw else d.count)
        d.active = max(n, 0)
        left_n -= d.active
        left_bw -= d.active * bw


# ---- fitting rules ---------------------------------------------------------------------------

def _attr_ids(data: StaticData, prefix: str) -> list[int]:
    return [a.id for a in data.attributes.values() if re.fullmatch(prefix + r"\d+", a.name)]


_restriction_cache: dict[int, tuple[list[int], list[int]]] = {}


def ship_restrictions(t: Type, data: StaticData) -> tuple[set[int], set[int]]:
    key = id(data)
    if key not in _restriction_cache:
        _restriction_cache[key] = (_attr_ids(data, "canFitShipGroup"), _attr_ids(data, "canFitShipType"))
    gattrs, tattrs = _restriction_cache[key]
    groups = {int(t.attrs[a]) for a in gattrs if t.attrs.get(a)}
    types = {int(t.attrs[a]) for a in tattrs if t.attrs.get(a)}
    return groups, types


def module_fits_ship(t: Type, ship: Type, data: StaticData) -> bool:
    """Static (skill-independent) check whether ``t`` can go on ``ship`` at all."""
    groups, types = ship_restrictions(t, data)
    if (groups or types) and ship.group_id not in groups and ship.id not in types:
        return False
    slot = slot_of(t)
    if slot == "rig":
        rs = t.attrs.get(C.A_RIG_SIZE)
        if rs is not None and rs != ship.attrs.get(C.A_RIG_SIZE):
            return False
        if not ship.attrs.get(C.A_RIG_SLOTS):
            return False
    if slot == "subsystem":
        return int(t.attrs.get(A_FITS_TO_SHIP_TYPE, 0)) == ship.id
    if slot == "mode":
        return t.name.startswith(ship.name + " ")
    if not ship.attrs.get(A_MAX_SUBSYSTEMS):  # T3 cruisers get hardpoints from subsystems
        if is_turret(t) and not ship.attrs.get(C.A_TURRET_SLOTS):
            return False
        if is_launcher(t) and not ship.attrs.get(C.A_LAUNCHER_SLOTS):
            return False
    return True


def charge_fits(module: Type, charge: Type) -> bool:
    groups = {int(module.attrs[a]) for a in CHARGE_GROUP_ATTRS if module.attrs.get(a)}
    if charge.group_id not in groups:
        return False
    # Tech II charges need a Tech II module. The charge-group lists do not encode this:
    # many faction, officer and storyline weapons list the advanced ammo groups too.
    if charge.attrs.get(A_TECH_LEVEL, 1) >= 2 and module.attrs.get(A_TECH_LEVEL, 1) < 2:
        return False
    size = module.attrs.get(A_CHARGE_SIZE)
    if size is not None and charge.attrs.get(A_CHARGE_SIZE) not in (None, size):
        return False
    cap = module.attrs.get(C.A_CAPACITY, 0.0)
    vol = charge.attrs.get(C.A_VOLUME, 0.0)
    return vol <= cap + 1e-9


def check(fit: Fit, char: Character, calc: FitCalc | None = None) -> list[str]:
    """Return a list of fitting-rule violations (empty means the fit is legal)."""
    data = char.data
    calc = calc or fit.calc(char)
    problems: list[str] = []

    def need(t: Type, what: str):
        miss = char.missing(t)
        if miss:
            names = ", ".join(f"{data.types[s].name} {lvl}" if s in data.types else f"skill {s} {lvl}"
                              for s, lvl in miss.items())
            problems.append(f"{what} {t.name} needs {names}")

    need(fit.ship, "ship")
    for m in fit.modules:
        need(m.type, "module")
        if m.charge is not None:
            need(m.charge, "charge")
            if not charge_fits(m.type, m.charge):
                problems.append(f"{m.charge.name} cannot be loaded in {m.type.name}")
        if not module_fits_ship(m.type, fit.ship, data):
            problems.append(f"{m.type.name} cannot be fitted to {fit.ship.name}")
    for d in fit.drones:
        need(d.type, "drone")
    modes = fit.by_slot("mode")
    if len(modes) > 1:
        problems.append("only one tactical mode can be active")
    elif not modes and ship_modes(fit.ship, data):
        problems.append(f"{fit.ship.name} needs a tactical mode (e.g. {ship_modes(fit.ship, data)[0].name})")

    ship = calc.ship
    for slot, attr in _SLOT_COUNT_ATTR.items():
        n = len(fit.by_slot(slot))
        have = int(calc.get(ship, attr))
        if n > have:
            problems.append(f"{n} {slot} modules but only {have} {slot} slots")
    turrets = sum(1 for m in fit.modules if is_turret(m.type))
    launchers = sum(1 for m in fit.modules if is_launcher(m.type))
    if turrets > calc.get(ship, C.A_TURRET_SLOTS):
        problems.append(f"{turrets} turrets but only {int(calc.get(ship, C.A_TURRET_SLOTS))} turret hardpoints")
    if launchers > calc.get(ship, C.A_LAUNCHER_SLOTS):
        problems.append(f"{launchers} launchers but only {int(calc.get(ship, C.A_LAUNCHER_SLOTS))} launcher hardpoints")

    res = resources(calc)
    for k in ("cpu", "power", "calibration"):
        used, out = res[k]
        if used > out + 1e-6:
            problems.append(f"{k} {used:.2f} / {out:.2f} exceeded")

    groups: dict[int, int] = {}
    active_groups: dict[int, int] = {}
    for m in fit.modules:
        groups[m.type.group_id] = groups.get(m.type.group_id, 0) + 1
        if m.state >= C.STATE_ACTIVE:
            active_groups[m.type.group_id] = active_groups.get(m.type.group_id, 0) + 1
    for m in fit.modules:
        mg = m.type.attrs.get(A_MAX_GROUP_FITTED)
        if mg and groups[m.type.group_id] > mg:
            problems.append(f"at most {int(mg)} of {m.type.group_name} may be fitted")
            groups[m.type.group_id] = 0  # report once
        ma = m.type.attrs.get(A_MAX_GROUP_ACTIVE)
        if ma and m.state >= C.STATE_ACTIVE and active_groups.get(m.type.group_id, 0) > ma:
            problems.append(f"at most {int(ma)} of {m.type.group_name} may be active")
            active_groups[m.type.group_id] = 0

    bw_used = sum(d.active * (d.type.attr(A_DRONE_BANDWIDTH_USED) or 0) for d in fit.drones)
    bay_used = sum(d.count * d.type.attrs.get(C.A_VOLUME, 0) for d in fit.drones)
    if bw_used > calc.get(ship, A_DRONE_BANDWIDTH) + 1e-6:
        problems.append("drone bandwidth exceeded")
    if bay_used > calc.get(ship, A_DRONE_CAPACITY) + 1e-6:
        problems.append("drone bay capacity exceeded")
    max_active = calc.char_attr("maxActiveDrones")
    if sum(d.active for d in fit.drones) > max_active:
        problems.append(f"more than {int(max_active)} drones active")

    if fit.fighters:
        for f in fit.fighters:
            need(f.type, "fighter")
        launched = [f for f in fit.fighters if f.active]
        tubes = int(calc.get(ship, "fighterTubes"))
        if len(launched) > tubes:
            problems.append(f"{len(launched)} fighter squadrons but only {tubes} launch tubes")
        for role, attr in FIGHTER_TUBE_ATTRS.items():
            n = sum(1 for f in launched if f.role == role)
            have = int(calc.get(ship, attr))
            if n > have:
                problems.append(f"{n} {role} fighter squadrons but only {have} {role} tubes")
        bay = sum(f.amount * f.type.attrs.get(C.A_VOLUME, 0) for f in fit.fighters)
        if bay > calc.get(ship, "fighterCapacity") + 1e-6:
            problems.append("fighter bay capacity exceeded")
        for f, it in zip(launched, calc.fighters):
            size = int(calc.get(it, "fighterSquadronMaxSize"))
            if f.amount > size:
                problems.append(f"{f.type.name} squadrons hold at most {size} fighters")
    return problems


def resources(calc: FitCalc) -> dict[str, tuple[float, float]]:
    cpu = power = cal = 0.0
    for m in calc.modules:
        if m.slot == "rig":
            cal += calc.get(m, C.A_UPGRADE_COST)
        if m.state >= C.STATE_ONLINE:
            cpu += calc.get(m, C.A_CPU)
            power += calc.get(m, C.A_POWER)
    ship = calc.ship
    return {"cpu": (round(cpu, 2), calc.get(ship, C.A_CPU_OUTPUT)),
            "power": (round(power, 2), calc.get(ship, C.A_POWER_OUTPUT)),
            "calibration": (cal, calc.get(ship, C.A_UPGRADE_CAPACITY))}
