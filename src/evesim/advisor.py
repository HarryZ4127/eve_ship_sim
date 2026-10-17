"""Skill training advisor: which skills improve a fit (or the best fit) most per day trained.

Two analyses:

* **fit**: the fit stays fixed; every skill not yet at V is raised one level (with any
  missing prerequisites) and the objective is re-scored. This finds the direct bonus
  skills: gunnery/missile/drone damage, tank, fitting, navigation, hull skills.
* **refit**: skills that *unlock* better items (T2 modules, ammo, drones) do nothing for
  the current fit, so for the most promising unlocks the optimizer is re-run with the
  skill trained, and the gain of the re-optimized fit is reported.

``plan`` chains the best fit-mode steps greedily within a training-day budget.

Training time uses EVE's rules: skill points for level L are
``250 * rank * sqrt(32) ** (L - 1)`` and a character trains
``primary + secondary / 2`` SP per minute.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from evesim.data.store import CAT_CHARGE, CAT_DRONE, CAT_MODULE, StaticData, Type
from evesim.dogma.engine import Character
from evesim.fit import Fit, module_fits_ship
from evesim.market import Prices
from evesim.optimize.search import Optimizer
from evesim.optimize.spec import METRICS, Spec

ATTRIBUTE_IDS = {"charisma": 164, "intelligence": 165, "memory": 166, "perception": 167, "willpower": 168}
ATTRIBUTE_NAMES = {v: k for k, v in ATTRIBUTE_IDS.items()}
DEFAULT_ATTRIBUTES = {"charisma": 20, "intelligence": 20, "memory": 20, "perception": 20, "willpower": 20}
A_PRIMARY, A_SECONDARY, A_RANK = 180, 181, 275


def sp_for_level(rank: float, level: int) -> float:
    if level <= 0:
        return 0.0
    return round(250 * rank * math.sqrt(32) ** (level - 1))


def sp_per_minute(skill: Type, attributes: dict[str, float]) -> float:
    prim = ATTRIBUTE_NAMES.get(int(skill.attrs.get(A_PRIMARY, 0)), "perception")
    sec = ATTRIBUTE_NAMES.get(int(skill.attrs.get(A_SECONDARY, 0)), "willpower")
    return attributes.get(prim, 20) + attributes.get(sec, 20) / 2.0


def train_seconds(skill: Type, from_level: int, to_level: int, attributes: dict[str, float]) -> float:
    rank = skill.attrs.get(A_RANK, 1.0)
    sp = sp_for_level(rank, to_level) - sp_for_level(rank, from_level)
    return max(sp, 0.0) / sp_per_minute(skill, attributes) * 60.0


def steps_to(data: StaticData, levels: dict[int, int], skill_id: int, level: int) -> list[tuple[int, int, int]]:
    """Ordered (skill, from, to) steps to reach ``skill_id`` at ``level``, prerequisites first."""
    out: list[tuple[int, int, int]] = []
    want = dict(levels)

    def need(sid: int, lvl: int):
        if want.get(sid, 0) >= lvl:
            return
        t = data.types.get(sid)
        if t is None:
            raise KeyError(f"unknown skill {sid}")
        for pre, plvl in t.required_skills.items():
            need(pre, plvl)
        out.append((sid, want.get(sid, 0), lvl))
        want[sid] = lvl

    need(skill_id, level)
    return out


@dataclass
class Suggestion:
    skill: str
    from_level: int
    to_level: int
    steps: list[tuple[str, int, int]]   # including prerequisites
    seconds: float
    gain: float                          # objective points (see the spec's scales)
    before: dict[str, float]
    after: dict[str, float]
    kind: str = "bonus"                  # "bonus" (helps this fit) or "unlock" (enables a better fit)
    unlocks: list[str] = field(default_factory=list)
    fit_eft: str | None = None           # the re-optimized fit, for unlocks

    @property
    def days(self) -> float:
        return self.seconds / 86400.0

    @property
    def gain_per_day(self) -> float:
        return self.gain / max(self.days, 1e-6)


class Advisor:
    def __init__(self, data: StaticData, levels: dict[int, int], spec: Spec, implants=(),
                 attributes: dict[str, float] | None = None, prices: Prices | None = None, log=lambda m: None):
        self.data = data
        self.levels = {int(k): int(v) for k, v in levels.items()}
        self.spec = spec
        self.implants = list(implants)
        self.attributes = dict(DEFAULT_ATTRIBUTES, **(attributes or {}))
        self.prices = prices or Prices(data)
        self.log = log

    # ---- scoring ----------------------------------------------------------------------------
    def _char(self, levels) -> Character:
        return Character(self.data, levels, self.implants)

    def score_fit(self, fit: Fit, levels) -> tuple[float, dict[str, float]]:
        opt = Optimizer(self.data, self._char(levels), self.spec, self.prices)
        ev = opt.evaluate(fit, sim_cap=self.spec.cap_simulation_needed)
        return ev.score, ev.metrics

    def _apply(self, steps) -> dict[int, int]:
        lv = dict(self.levels)
        for sid, _, to in steps:
            lv[sid] = max(lv.get(sid, 0), to)
        return lv

    def _suggestion(self, sid, steps, gain, before, after, kind="bonus", unlocks=(), eft=None) -> Suggestion:
        secs = sum(train_seconds(self.data.types[s], a, b, self.attributes) for s, a, b in steps)
        t = self.data.types[sid]
        return Suggestion(t.name, self.levels.get(sid, 0), steps[-1][2],
                          [(self.data.types[s].name, a, b) for s, a, b in steps], secs, gain, before, after,
                          kind, list(unlocks), eft)

    # ---- fit mode ---------------------------------------------------------------------------
    def fit_suggestions(self, fit: Fit, max_days: float = 60.0) -> list[Suggestion]:
        base, base_m = self.score_fit(fit, self.levels)
        out = []
        for t in self.data.skills:
            cur = self.levels.get(t.id, 0)
            if cur >= 5 or not any(e.modifiers for e in t.effects):
                continue
            steps = steps_to(self.data, self.levels, t.id, cur + 1)
            secs = sum(train_seconds(self.data.types[s], a, b, self.attributes) for s, a, b in steps)
            if secs > max_days * 86400:
                continue
            sc, m = self.score_fit(fit, self._apply(steps))
            if sc <= base + 1e-9:
                continue
            if len(steps) > 1:
                # Credit only the skill itself: its prerequisites are listed on their own.
                pre, _ = self.score_fit(fit, self._apply(steps[:-1]))
                if sc <= pre + 1e-9:
                    continue
            out.append(self._suggestion(t.id, steps, sc - base, base_m, m))
        out.sort(key=lambda s: -s.gain_per_day)
        return out

    def plan(self, fit: Fit, days: float, max_steps: int = 25) -> tuple[list[Suggestion], dict[str, float], dict[str, float]]:
        """Greedy training plan for a fixed fit within ``days``."""
        saved = self.levels
        plan: list[Suggestion] = []
        left = days * 86400.0
        _, start_m = self.score_fit(fit, self.levels)
        candidates = None
        try:
            for _ in range(max_steps):
                sugg = self.fit_suggestions(fit, max_days=left / 86400.0)
                if candidates is not None:
                    sugg = [s for s in sugg if s.skill in candidates] or sugg
                sugg = [s for s in sugg if s.seconds <= left]
                if not sugg:
                    break
                best = sugg[0]
                candidates = {s.skill for s in sugg}
                plan.append(best)
                left -= best.seconds
                self.levels = self._apply([(self.data.type_by_name(n).id, a, b) for n, a, b in best.steps])
            _, end_m = self.score_fit(fit, self.levels)
        finally:
            self.levels = saved
        return plan, start_m, end_m

    # ---- refit mode -------------------------------------------------------------------------
    def unlock_candidates(self, ship: Type, max_days: float, limit: int,
                          fit: Fit | None = None) -> list[tuple[list, list[str]]]:
        """Skill sets that would unlock relevant modules/ammo/drones for ``ship``, best first:
        upgrades of items already in ``fit`` (T2 launchers, ammo, drones ...) lead."""
        family = lambda t: t.variation_parent or t.id  # noqa: E731
        fitted = set()
        if fit is not None:
            for m in fit.modules:
                fitted.add(family(m.type))
                if m.charge is not None:
                    fitted.add(family(m.charge))
                    fitted.add(("charge-group", m.charge.group_id))
            for d in fit.drones:
                fitted.add(family(d.type))
        probe = Optimizer(self.data, self._char(self.levels), self.spec, self.prices)
        meta = probe.allowed_meta | probe.allowed_ammo_meta
        groups: dict[tuple, list[Type]] = {}
        char = probe.char
        for t in self.data.types.values():
            if t.category_id not in (CAT_MODULE, CAT_CHARGE, CAT_DRONE) or t.market_group is None:
                continue
            if t.meta_group not in meta or char.meets(t):
                continue
            if t.category_id == CAT_MODULE and not module_fits_ship(t, ship, self.data):
                continue
            if not (probe._touched(t) & probe.relevant_attr_ids) and t.category_id == CAT_MODULE:
                continue
            missing = char.missing(t)
            steps = []
            lv = dict(self.levels)
            for sid, lvl in sorted(missing.items()):
                st = steps_to(self.data, lv, sid, lvl)
                steps += st
                for s, _, b in st:
                    lv[s] = b
            key = tuple(sorted({(s, b) for s, _, b in steps}))
            groups.setdefault(key, []).append(t)
        ranked = []
        for key, types in groups.items():
            steps = [(s, self.levels.get(s, 0), b) for s, b in key]
            secs = sum(train_seconds(self.data.types[s], a, b, self.attributes) for s, a, b in steps)
            if secs <= max_days * 86400:
                upgrades = sum(1 for t in types if family(t) in fitted or ("charge-group", t.group_id) in fitted)
                ranked.append((secs, len(types), steps, [t.name for t in types], upgrades))
        # Upgrades of fitted items first (fastest first), then most items unlocked per day.
        ranked.sort(key=lambda r: (0, r[0]) if r[4] else (1, -r[1] / max(r[0] / 86400.0, 0.5)))
        return [(r[2], r[3]) for r in ranked[:limit]]

    def refit_suggestions(self, max_days: float = 60.0, limit: int = 8) -> tuple[list[Suggestion], object]:
        opt = Optimizer(self.data, self._char(self.levels), self.spec, self.prices)
        base_results = opt.run()
        base = base_results[0]
        if base.best is None:
            raise ValueError("; ".join(base.notes) or "no fit found with current skills")
        runs = []
        for steps, unlocked in self.unlock_candidates(base.ship, max_days, limit, base.best.fit):
            levels = self._apply(steps)
            self.log(f"re-optimizing with {', '.join(f'{self.data.types[s].name} {b}' for s, _, b in steps)}")
            r = Optimizer(self.data, self._char(levels), self.spec, self.prices).run_ship(base.ship)
            if r.best is not None:
                runs.append((steps, unlocked, r.best))
        # The search is heuristic, so a re-run can find a better fit that never needed the new
        # skill. The reference is the best fit found by any run that the current skills can fly;
        # a skill is credited only for what it adds beyond that.
        old_char = self._char(self.levels)
        reference = base.best
        for _, _, ev in runs:
            fit = ev.fit
            if all(old_char.meets(m.type) and (m.charge is None or old_char.meets(m.charge)) for m in fit.modules) \
                    and all(old_char.meets(d.type) for d in fit.drones):
                again = opt.evaluate(fit)
                if again.violation <= reference.violation and again.objective > reference.objective:
                    reference = again
        out = []
        for steps, unlocked, ev in runs:
            gain = ev.objective - reference.objective
            if ev.violation > reference.violation + 1e-9 or gain <= 1e-3:
                continue
            out.append(self._suggestion(steps[-1][0], steps, gain, reference.metrics, ev.metrics, "unlock",
                                        unlocked[:5], ev.fit.to_eft()))
        self.reference = reference
        out.sort(key=lambda s: -s.gain_per_day)
        return out, base


def objective_metrics(spec: Spec) -> list[str]:
    return list(spec.weights) + [c.metric for c in spec.constraints if c.metric not in spec.weights]


def describe_change(s: Suggestion, spec: Spec) -> str:
    parts = []
    for m in objective_metrics(spec):
        a, b = s.before.get(m), s.after.get(m)
        if a is None or b is None or a == b or math.isinf(a) or math.isinf(b):
            continue
        pct = f" ({(b - a) / abs(a) * 100:+.1f}%)" if a else ""
        parts.append(f"{m} {a:,.1f} -> {b:,.1f}{METRICS[m].unit and ' ' + METRICS[m].unit}{pct}")
    return "; ".join(parts)
