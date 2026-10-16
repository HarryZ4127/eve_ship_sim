"""Generic dogma engine driven by the static data's ``modifierInfo``.

A :class:`Character` holds the skills and implants (its values are computed
once and reused across fits); :class:`FitCalc` combines it with a ship, modules,
charges and drones and resolves modified attribute values lazily.

Rules implemented:

* operation order: preAssign, preMul, preDiv, modAdd, modSub, postMul,
  postDiv, postPercent, postAssign;
* stacking penalties for non-stackable attributes on multiplicative operations,
  per operation, with the usual ``exp(-(i/2.67)^2)`` falloff, positive and
  negative modifiers penalized separately; ships, charges, skills, implants and
  subsystems are exempt as sources;
* min/max capping attributes;
* the handful of fitting-relevant effects CCP left without ``modifierInfo``
  (propulsion modules, subsystem slots/hardpoints, reactive armor hardener).
"""

from __future__ import annotations

import math
from typing import Callable, Iterable

from evesim.data.store import (CAT_CHARGE, CAT_IMPLANT, CAT_SHIP, CAT_SKILL, CAT_SUBSYSTEM,
                               StaticData, Type)
from evesim.dogma import const as C

PENALTY_EXEMPT_CATEGORIES = {CAT_SHIP, CAT_CHARGE, CAT_SKILL, CAT_IMPLANT, CAT_SUBSYSTEM}
PENALIZABLE_OPS = {C.OP_PRE_MUL, C.OP_PRE_DIV, C.OP_POST_MUL, C.OP_POST_DIV, C.OP_POST_PERCENT}
ROUNDED_ATTRS = {C.A_CPU, C.A_POWER, C.A_CPU_OUTPUT, C.A_POWER_OUTPUT}
_PENALTY_DENOM = 2.67 ** 2

# Effects whose categories gate on module state.
_STATE_EFFECT_CATEGORIES = {
    C.STATE_OFFLINE: {C.EC_PASSIVE},
    C.STATE_ONLINE: {C.EC_PASSIVE, C.EC_ONLINE},
    C.STATE_ACTIVE: {C.EC_PASSIVE, C.EC_ONLINE, C.EC_ACTIVE},
    C.STATE_OVERLOAD: {C.EC_PASSIVE, C.EC_ONLINE, C.EC_ACTIVE, C.EC_OVERLOAD},
}


class Item:
    """One dogma item (character, skill, ship, module, charge, drone ...)."""

    __slots__ = ("type", "kind", "state", "slot", "charge", "container", "amount", "values",
                 "req", "located", "owned", "level", "uid")

    def __init__(self, type_: Type, kind: str, state: int = C.STATE_ONLINE, slot: str | None = None):
        self.type = type_
        self.kind = kind
        self.state = state
        self.slot = slot
        self.charge: Item | None = None
        self.container: Item | None = None
        self.amount = 1
        self.values: dict[int, float] = {}
        self.req = set(type_.required_skills)
        self.located = {"module": "ship", "charge": "ship", "skill": "char", "implant": "char"}.get(kind)
        self.owned = kind in ("module", "charge", "drone")
        self.level = 0
        self.uid = 0

    @property
    def category(self) -> int:
        return self.type.category_id

    def active_effects(self):
        cats = _STATE_EFFECT_CATEGORIES[self.state] if self.kind == "module" else {C.EC_PASSIVE}
        for e in self.type.effects:
            if e.category in cats:
                yield e

    def __repr__(self):
        return f"<Item {self.kind} {self.type.name!r}>"


class _Mod:
    __slots__ = ("op", "src", "src_attr", "fn", "penalize_ok")

    def __init__(self, op, src, src_attr=None, fn=None):
        self.op = op
        self.src = src
        self.src_attr = src_attr
        self.fn = fn
        self.penalize_ok = src.category not in PENALTY_EXEMPT_CATEGORIES


class _Index:
    """Modifiers keyed by how their targets are selected."""

    __slots__ = ("direct", "loc_all", "loc_group", "loc_skill", "owner_skill", "deferred")

    def __init__(self):
        self.direct: dict[tuple[int, int], list[_Mod]] = {}
        self.loc_all: dict[tuple[str, int], list[_Mod]] = {}
        self.loc_group: dict[tuple[str, int, int], list[_Mod]] = {}
        self.loc_skill: dict[tuple[str, int, int], list[_Mod]] = {}
        self.owner_skill: dict[tuple[int, int], list[_Mod]] = {}
        # (domain, attr, mod) for ItemModifiers on the ship/char, resolved per fit.
        self.deferred: list[tuple[str, int, _Mod]] = []

    def add(self, src: Item, m, resolve_item: Callable[[str, Item], Item | None]):
        if m.func == "EffectStopper" or m.modified is None:
            return
        op = m.op
        if op is None or op == C.OP_SKILL_LEVEL_CALC:
            return
        mod = _Mod(op, src, m.modifying, getattr(m, "fn", None))
        if m.func == "ItemModifier":
            if m.domain in ("shipID", "charID") and resolve_item is None:
                self.deferred.append((m.domain, m.modified, mod))
                return
            tgt = resolve_item(m.domain, src) if resolve_item else None
            if tgt is not None:
                self.direct.setdefault((id(tgt), m.modified), []).append(mod)
            return
        loc = {"shipID": "ship", "charID": "char"}.get(m.domain)
        if loc is None:
            return
        if m.func == "LocationModifier":
            self.loc_all.setdefault((loc, m.modified), []).append(mod)
        elif m.func == "LocationGroupModifier":
            self.loc_group.setdefault((loc, m.group, m.modified), []).append(mod)
        elif m.func == "LocationRequiredSkillModifier":
            skill = src.type.id if m.skill == -1 else m.skill
            self.loc_skill.setdefault((loc, skill, m.modified), []).append(mod)
        elif m.func == "OwnerRequiredSkillModifier":
            skill = src.type.id if m.skill == -1 else m.skill
            self.owner_skill.setdefault((skill, m.modified), []).append(mod)

    def collect(self, item: Item, attr: int, out: list):
        d = self.direct.get((id(item), attr))
        if d:
            out.extend(d)
        loc = item.located
        if loc:
            x = self.loc_all.get((loc, attr))
            if x:
                out.extend(x)
            x = self.loc_group.get((loc, item.type.group_id, attr))
            if x:
                out.extend(x)
            for s in item.req:
                x = self.loc_skill.get((loc, s, attr))
                if x:
                    out.extend(x)
        if item.owned:
            for s in item.req:
                x = self.owner_skill.get((s, attr))
                if x:
                    out.extend(x)


class _Calculator:
    """Shared attribute resolution for Character and FitCalc."""

    data: StaticData

    def _indexes(self) -> Iterable[_Index]:
        raise NotImplementedError

    def _extra(self, item: Item, attr: int, out: list):
        pass

    def get(self, item: Item, attr: int | str) -> float:
        if isinstance(attr, str):
            attr = self.data.attr_id(attr)
        v = item.values.get(attr)
        if v is None:
            item.values[attr] = item.type.attr(attr)  # cycle guard: base value
            v = self._compute(item, attr)
            item.values[attr] = v
        return v

    def _compute(self, item: Item, attr: int) -> float:
        info = self.data.attributes.get(attr)
        if item.kind == "skill" and attr == C.A_SKILL_LEVEL:
            return float(item.level)
        base = item.type.attrs.get(attr)
        if base is None:
            base = info.default if info else 0.0
        mods: list[_Mod] = []
        for idx in self._indexes():
            idx.collect(item, attr, mods)
        self._extra(item, attr, mods)
        val = base
        if mods:
            val = self._apply(item, attr, info, base, mods)
        if info is not None:
            if info.max_attr:
                val = min(val, self.get(item, info.max_attr))
            if info.min_attr:
                val = max(val, self.get(item, info.min_attr))
        if attr in ROUNDED_ATTRS:
            val = round(val, 2)
        return val

    def _apply(self, item, attr, info, base, mods):
        by_op: dict[int, list[tuple[float, bool]]] = {}
        stackable = info.stackable if info else True
        for m in mods:
            v = m.fn(self) if m.fn else self.get(m.src, m.src_attr)
            pen = (not stackable) and m.penalize_ok and m.op in PENALIZABLE_OPS
            by_op.setdefault(m.op, []).append((v, pen))
        val = base
        high = info.high_is_good if info else True
        for op in C.OP_ORDER:
            vals = by_op.get(op)
            if not vals:
                continue
            if op in (C.OP_PRE_ASSIGN, C.OP_POST_ASSIGN):
                xs = [v for v, _ in vals]
                val = max(xs) if high else min(xs)
            elif op == C.OP_MOD_ADD:
                val += sum(v for v, _ in vals)
            elif op == C.OP_MOD_SUB:
                val -= sum(v for v, _ in vals)
            else:
                plain, pen = [], []
                for v, p in vals:
                    if op == C.OP_POST_PERCENT:
                        mult = 1.0 + v / 100.0
                    elif op in (C.OP_PRE_DIV, C.OP_POST_DIV):
                        mult = 1.0 / v if v else 1.0
                    else:
                        mult = v
                    (pen if p else plain).append(mult)
                for mult in plain:
                    val *= mult
                if pen:
                    val *= _penalized_product(pen)
        return val


def _penalized_product(mults: list[float]) -> float:
    out = 1.0
    for group in ([m for m in mults if m > 1.0], [m for m in mults if m < 1.0]):
        group.sort(key=lambda m: -abs(m - 1.0))
        for i, m in enumerate(group):
            out *= 1.0 + (m - 1.0) * math.exp(-(i * i) / _PENALTY_DENOM)
    return out


class Character(_Calculator):
    """Skills and implants. Skill values are computed once per character."""

    def __init__(self, data: StaticData, skills: dict[int, int], implants: Iterable[int] = (), name: str = "character"):
        self.data = data
        self.name = name
        self.skill_levels = {int(k): int(v) for k, v in skills.items() if v and int(k) in data.types}
        self.skill_items: dict[int, Item] = {}
        for sid, lvl in self.skill_levels.items():
            it = Item(data.types[sid], "skill")
            it.level = lvl
            self.skill_items[sid] = it
        self.implant_items = [Item(data.types[i], "implant") for i in implants if i in data.types]
        self.index = _Index()
        for it in self._static_items():
            for e in it.active_effects():
                if e.id == C.E_SKILL_EFFECT:
                    continue
                mods = e.modifiers or (_skill_self_mods(self, it, e.id) if it.kind == "skill" else [])
                for m in mods:
                    self.index.add(it, m, None if m.domain in ("shipID", "charID") else self._resolve)
        # Skill and implant values never depend on the fit; compute what modifiers read eagerly.
        for it in self._static_items():
            for e in it.active_effects():
                for m in e.modifiers:
                    if m.modifying is not None:
                        self.get(it, m.modifying)

    def _static_items(self):
        yield from self.skill_items.values()
        yield from self.implant_items

    def _resolve(self, domain, src):
        return src if domain == "itemID" else None

    def _indexes(self):
        return (self.index,)

    def level(self, skill_id: int) -> int:
        return self.skill_levels.get(skill_id, 0)

    def meets(self, type_: Type) -> bool:
        return all(self.skill_levels.get(s, 0) >= lvl for s, lvl in type_.required_skills.items())

    def missing(self, type_: Type) -> dict[int, int]:
        return {s: lvl for s, lvl in type_.required_skills.items() if self.skill_levels.get(s, 0) < lvl}


class FitCalc(_Calculator):
    """Modified attributes for one fit flown by one character."""

    def __init__(self, char: Character, ship: Type, modules: list[tuple[Type, int, str, Type | None]],
                 drones: list[tuple[Type, int]] = ()):
        self.data = char.data
        self.char = char
        self.char_item = Item(self.data.character_type, "char")
        self.ship = Item(ship, "ship")
        self.modules: list[Item] = []
        for t, state, slot, charge in modules:
            it = Item(t, "module", state, slot)
            if charge is not None:
                ch = Item(charge, "charge")
                ch.container = it
                it.charge = ch
            self.modules.append(it)
        self.drones: list[Item] = []
        for t, n in drones:
            d = Item(t, "drone")
            d.amount = n
            self.drones.append(d)
        self.index = _Index()
        self._dynamic: dict[tuple[int, int], list[_Mod]] = {}
        for it in self._fit_items():
            self._register(it)
        # Character-level ItemModifiers on ship/char resolve against this fit.
        for domain, attr, mod in char.index.deferred:
            tgt = self.ship if domain == "shipID" else self.char_item
            self.index.direct.setdefault((id(tgt), attr), []).append(mod)

    def _fit_items(self):
        yield self.ship
        for m in self.modules:
            yield m
            if m.charge:
                yield m.charge
        yield from self.drones

    def _resolve(self, domain, src):
        if domain == "shipID":
            return self.ship
        if domain == "charID":
            return self.char_item
        if domain == "itemID":
            return src
        if domain == "otherID":
            return src.charge if src.kind == "module" else src.container
        return None

    def _register(self, it: Item):
        for e in it.active_effects():
            for m in e.modifiers:
                self.index.add(it, m, self._resolve)
            special = _SPECIAL.get(e.id)
            if special:
                special(self, it)

    def _dyn(self, target: Item, attr: int, mod: _Mod):
        self._dynamic.setdefault((id(target), attr), []).append(mod)

    def _indexes(self):
        return (self.index, self.char.index)

    def _extra(self, item, attr, out):
        d = self._dynamic.get((id(item), attr))
        if d:
            out.extend(d)

    def get(self, item: Item, attr: int | str) -> float:
        if item.kind in ("skill", "implant"):
            return self.char.get(item, attr)
        return super().get(item, attr)

    # convenience
    def ship_attr(self, name: str) -> float:
        return self.get(self.ship, name)

    def char_attr(self, name: str) -> float:
        return self.get(self.char_item, name)


# --- effects without modifierInfo ----------------------------------------------------------

class _SynthMod:
    """Stand-in for a static-data modifier, for effects CCP ships without modifierInfo."""

    __slots__ = ("func", "domain", "modified", "modifying", "op", "group", "skill", "effect", "fn")

    def __init__(self, func, domain, modified, modifying, op, skill=-1, fn=None):
        self.func, self.domain, self.modified, self.modifying = func, domain, modified, modifying
        self.op, self.group, self.skill, self.effect, self.fn = op, None, skill, None, fn


# Skill effects of the form "items requiring this skill get +bonus% per level":
# effect id -> (func, modified attribute, bonus attribute)
_SKILL_SELF_EFFECTS = {
    660: [("OwnerRequiredSkillModifier", "emDamage", "damageMultiplierBonus")],
    661: [("OwnerRequiredSkillModifier", "explosiveDamage", "damageMultiplierBonus")],
    662: [("OwnerRequiredSkillModifier", "thermalDamage", "damageMultiplierBonus")],
    668: [("OwnerRequiredSkillModifier", "kineticDamage", "damageMultiplierBonus")],
    1730: [("OwnerRequiredSkillModifier", "damageMultiplier", "damageMultiplierBonus")],
    1851: [("LocationRequiredSkillModifier", "speed", "rofBonus")],
    848: [("LocationRequiredSkillModifier", "cloakingTargetingDelay", "cloakingTargetingDelayBonus")],
}


def _skill_self_mods(char: "Character", skill: Item, eid: int) -> list[_SynthMod]:
    spec = _SKILL_SELF_EFFECTS.get(eid)
    if not spec:
        return []
    data = char.data
    # Some skills already scale the bonus attribute by level with a preMul effect.
    scaled = {m.modified for e in skill.type.effects for m in e.modifiers
              if m.func == "ItemModifier" and m.domain == "itemID" and m.modifying == C.A_SKILL_LEVEL}
    out = []
    for func, modified, bonus in spec:
        tgt, src = data.attr_id(modified), data.attr_id(bonus)
        fn = None
        if src not in scaled:
            fn = (lambda s, a: (lambda calc: calc.get(s, a) * s.level))(skill, src)
        domain = "charID" if func == "OwnerRequiredSkillModifier" else "shipID"
        out.append(_SynthMod(func, domain, tgt, src, C.OP_POST_PERCENT, -1, fn))
    return out


def _propulsion(calc: FitCalc, mod: Item, mwd: bool):
    ship = calc.ship
    calc._dyn(ship, C.A_MASS, _Mod(C.OP_MOD_ADD, mod, C.A_MASS_ADDITION))

    def speed(c: FitCalc):
        boost = c.get(mod, C.A_SPEED_FACTOR) * c.get(mod, C.A_SPEED_BOOST_FACTOR) / c.get(ship, C.A_MASS)
        return 1.0 + boost / 100.0

    calc._dyn(ship, C.A_MAX_VELOCITY, _Mod(C.OP_POST_MUL, mod, fn=speed))
    if mwd:
        calc._dyn(ship, C.A_SIGNATURE_RADIUS, _Mod(C.OP_POST_PERCENT, mod, C.A_SIGNATURE_RADIUS_BONUS))


def _subsystem_slots(calc: FitCalc, it: Item):
    for tgt, src in ((C.A_HI_SLOTS, C.A_HI_SLOT_MOD), (C.A_MED_SLOTS, C.A_MED_SLOT_MOD), (C.A_LOW_SLOTS, C.A_LOW_SLOT_MOD)):
        calc._dyn(calc.ship, tgt, _Mod(C.OP_MOD_ADD, it, src))


def _subsystem_hardpoints(calc: FitCalc, it: Item):
    calc._dyn(calc.ship, C.A_TURRET_SLOTS, _Mod(C.OP_MOD_ADD, it, C.A_TURRET_HP_MOD))
    calc._dyn(calc.ship, C.A_LAUNCHER_SLOTS, _Mod(C.OP_MOD_ADD, it, C.A_LAUNCHER_HP_MOD))


def _reactive_hardener(calc: FitCalc, it: Item):
    # Modelled at its starting split (equal resists); adaptation is not simulated.
    for a in C.ARMOR_RESONANCES:
        calc._dyn(calc.ship, a, _Mod(C.OP_PRE_MUL, it, a))


_SPECIAL: dict[int, Callable[[FitCalc, Item], None]] = {
    C.E_MWD: lambda c, i: _propulsion(c, i, True),
    C.E_AB: lambda c, i: _propulsion(c, i, False),
    C.E_SLOT_MODIFIER: _subsystem_slots,
    C.E_HARDPOINT_MODIFIER: _subsystem_hardpoints,
    C.E_ADAPTIVE_ARMOR_HARDENER: _reactive_hardener,
}
