"""Skill profiles: where a character's skill levels come from.

Sources:
* presets: every skill at a level (``all5``, ``all0`` ...) or an alpha clone grade;
* a JSON file, either our own format ``{"name": ..., "skills": {"Gunnery": 5, ...},
  "implants": [...]}`` (names or type IDs) or a saved ESI ``/characters/{id}/skills``
  response (``active_skill_level`` is used, so alpha caps apply);
* ESI directly, see :mod:`evesim.skills.esi`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from evesim.data.store import StaticData
from evesim.dogma.engine import Character


@dataclass
class SkillProfile:
    name: str
    levels: dict[int, int]
    implants: list[int] = field(default_factory=list)
    source: str = ""
    attributes: dict[str, float] = field(default_factory=dict)  # charisma, intelligence, ... (training speed)

    def character(self, data: StaticData) -> Character:
        return Character(data, self.levels, self.implants, self.name)

    def to_json(self, data: StaticData | None = None) -> dict:
        def key(sid):
            if data and sid in data.types:
                return data.types[sid].name
            return str(sid)
        return {"name": self.name, "source": self.source,
                "skills": {key(s): lvl for s, lvl in sorted(self.levels.items())},
                "implants": [data.types[i].name if data and i in data.types else i for i in self.implants],
                "attributes": self.attributes}

    def save(self, path: str | Path, data: StaticData | None = None):
        Path(path).write_text(json.dumps(self.to_json(data), indent=1))

    @property
    def total_levels(self) -> int:
        return sum(self.levels.values())


def all_level(data: StaticData, level: int) -> SkillProfile:
    return SkillProfile(f"All {level}", {s.id: level for s in data.skills}, source=f"preset:all{level}")


def clone_grade(data: StaticData, name: str) -> SkillProfile:
    """Alpha clone skill caps, e.g. ``alpha caldari``."""
    want = name.lower().replace("-", " ").replace("_", " ")
    for g in data.clone_grades.values():
        if g["name"].lower() == want:
            return SkillProfile(g["name"], {int(k): v for k, v in g["skills"].items()}, source=f"preset:{want}")
    names = ", ".join(g["name"] for g in data.clone_grades.values())
    raise KeyError(f"unknown clone grade {name!r}; choose from: {names}")


def _resolve(data: StaticData, key) -> int:
    if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
        return int(key)
    return data.type_by_name(key).id


def from_dict(data: StaticData, d: dict, name: str = "character", source: str = "") -> SkillProfile:
    skills = d.get("skills", d)
    implants = [_resolve(data, i) for i in d.get("implants", [])] if isinstance(d, dict) else []
    if isinstance(skills, list):  # ESI format
        levels = {int(s["skill_id"]): int(s.get("active_skill_level", s.get("trained_skill_level", 0)))
                  for s in skills}
    else:
        levels = {_resolve(data, k): int(v) for k, v in skills.items()}
    attrs = {k: float(v) for k, v in (d.get("attributes") or {}).items()
             if k in ("charisma", "intelligence", "memory", "perception", "willpower")} if isinstance(d, dict) else {}
    return SkillProfile(d.get("name", name) if isinstance(d, dict) else name, levels, implants, source, attrs)


def load(data: StaticData, spec: str) -> SkillProfile:
    """Resolve a ``--skills`` argument: ``all5``, ``alpha caldari``, a JSON path,
    or ``esi:<character name or id>`` for a character logged in with ``evesim login``."""
    s = spec.strip()
    low = s.lower()
    if low.startswith("all") and low[3:].strip().isdigit():
        return all_level(data, int(low[3:]))
    if low.startswith("alpha"):
        return clone_grade(data, s)
    if low == "esi" or low.startswith("esi:"):
        from evesim.skills import esi
        return esi.fetch_profile(data, s[4:].strip() or None)
    p = Path(s)
    if p.exists():
        d = json.loads(p.read_text())
        return from_dict(data, d, name=p.stem, source=str(p))
    from evesim.skills import esi
    logged_in = esi.characters()
    if any(c["name"].lower() == low for c in logged_in):  # a logged-in character's bare name
        return esi.fetch_profile(data, s)
    hint = f" (logged in: {', '.join(c['name'] for c in logged_in)})" if logged_in else ""
    raise ValueError(f"cannot resolve skills {spec!r}: use all0..all5, 'alpha <race>', esi:<name>, "
                     f"or a JSON file{hint}")
