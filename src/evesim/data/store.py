"""In-memory static data: types, dogma attributes and effects."""

from __future__ import annotations

import gzip
import json
import os
from functools import cached_property
from pathlib import Path

# Required-skill attribute pairs (skill type, level).
REQUIRED_SKILL_ATTRS = ((182, 277), (183, 278), (184, 279), (1285, 1286), (1289, 1287), (1290, 1288))

CAT_SHIP, CAT_MODULE, CAT_CHARGE, CAT_SKILL, CAT_DRONE, CAT_IMPLANT, CAT_SUBSYSTEM = 6, 7, 8, 16, 18, 20, 32
CAT_FIGHTER = 87


def default_data_path() -> Path:
    env = os.environ.get("EVESIM_DATA")
    if env:
        return Path(env)
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "evesim" / "staticdata.json.gz"


class Attribute:
    __slots__ = ("id", "name", "default", "stackable", "high_is_good", "max_attr", "min_attr", "display", "unit")

    def __init__(self, aid, rec):
        self.id = aid
        (self.name, self.default, stackable, high, self.max_attr, self.min_attr, self.display, self.unit) = rec
        self.stackable = bool(stackable)
        self.high_is_good = bool(high)


class Modifier:
    __slots__ = ("func", "domain", "modified", "modifying", "op", "group", "skill", "effect")

    def __init__(self, rec):
        self.func, self.domain, self.modified, self.modifying, self.op, self.group, self.skill, self.effect = rec


class Effect:
    __slots__ = ("id", "name", "category", "modifiers", "duration_attr", "discharge_attr",
                 "range_attr", "falloff_attr", "tracking_attr")

    def __init__(self, eid, rec):
        self.id = eid
        self.name = rec["n"]
        self.category = rec["c"]
        self.modifiers = [Modifier(m) for m in rec["m"]]
        self.duration_attr = rec.get("dur")
        self.discharge_attr = rec.get("dis")
        self.range_attr = rec.get("rng")
        self.falloff_attr = rec.get("fal")
        self.tracking_attr = rec.get("trk")


class Type:
    __slots__ = ("id", "name", "group_id", "category_id", "attrs", "effect_ids", "default_effect",
                 "market_group", "meta_group", "variation_parent", "base_price", "race", "faction",
                 "required_skills", "_data")

    def __init__(self, tid, rec, data: "StaticData"):
        self.id = tid
        self.name = rec["n"]
        self.group_id = rec["g"]
        g = data.groups.get(self.group_id)
        self.category_id = g[1] if g else 1
        self.attrs = {int(k): v for k, v in rec["a"].items()}
        self.effect_ids = rec["e"]
        self.default_effect = rec.get("de")
        self.market_group = rec.get("mg")
        self.meta_group = rec.get("mt")
        self.variation_parent = rec.get("vp")
        self.base_price = rec.get("p") or 0.0
        self.race = rec.get("race")
        self.faction = rec.get("fac")
        req = {}
        for sa, la in REQUIRED_SKILL_ATTRS:
            s = self.attrs.get(sa)
            if s:
                req[int(s)] = int(self.attrs.get(la, 0))
        self.required_skills = req
        self._data = data

    def attr(self, name_or_id, default=None):
        aid = name_or_id if isinstance(name_or_id, int) else self._data.attr_id(name_or_id)
        if aid in self.attrs:
            return self.attrs[aid]
        if default is not None:
            return default
        a = self._data.attributes.get(aid)
        return a.default if a else 0.0

    @property
    def group_name(self):
        return self._data.groups.get(self.group_id, ("?", 0))[0]

    @property
    def effects(self):
        return [self._data.effects[e] for e in self.effect_ids if e in self._data.effects]

    def has_effect(self, eid):
        return eid in self.effect_ids

    @property
    def meta_level(self):
        return int(self.attrs.get(633, 0))  # metaLevelOld

    def __repr__(self):
        return f"<Type {self.id} {self.name!r}>"


class StaticData:
    def __init__(self, raw: dict):
        self.meta = raw.get("meta", {})
        self.attributes = {int(k): Attribute(int(k), v) for k, v in raw["attributes"].items()}
        self._attr_by_name = {a.name: a.id for a in self.attributes.values()}
        self.effects = {int(k): Effect(int(k), v) for k, v in raw["effects"].items()}
        self.groups = {int(k): tuple(v) for k, v in raw["groups"].items()}
        self.categories = {int(k): v for k, v in raw["categories"].items()}
        self.types = {int(k): Type(int(k), v, self) for k, v in raw["types"].items()}
        self.clone_grades = raw.get("clone_grades", {})
        self._by_name = {}
        for t in self.types.values():
            self._by_name.setdefault(t.name.lower(), t)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "StaticData":
        path = Path(path) if path else default_data_path()
        if not path.exists():
            raise FileNotFoundError(
                f"static data not found at {path}; run `evesim data update` first")
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as f:
            return cls(json.load(f))

    def attr_id(self, name: str) -> int:
        try:
            return self._attr_by_name[name]
        except KeyError:
            raise KeyError(f"unknown dogma attribute {name!r}") from None

    def type_by_name(self, name: str) -> Type:
        t = self._by_name.get(name.strip().lower())
        if t is None:
            raise KeyError(f"unknown type {name!r}")
        return t

    def find_types(self, text: str, category: int | None = None, limit: int = 20) -> list[Type]:
        text = text.lower()
        out = [t for t in self.types.values()
               if text in t.name.lower() and (category is None or t.category_id == category)]
        out.sort(key=lambda t: (not t.name.lower().startswith(text), len(t.name)))
        return out[:limit]

    def of_category(self, cat: int) -> list[Type]:
        return [t for t in self.types.values() if t.category_id == cat]

    @cached_property
    def skills(self) -> list[Type]:
        return self.of_category(CAT_SKILL)

    @cached_property
    def character_type(self) -> Type:
        return self.types[1373]


_cache: dict[str, StaticData] = {}


def get_data(path: str | Path | None = None) -> StaticData:
    key = str(path or default_data_path())
    if key not in _cache:
        _cache[key] = StaticData.load(path)
    return _cache[key]
