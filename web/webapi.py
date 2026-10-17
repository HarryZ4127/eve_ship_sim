"""Bridge between the browser demo (web/index.html) and evesim, run under Pyodide."""

import json
import math

from evesim.application import PRESETS, Target, applied_dps
from evesim.data.store import CAT_CHARGE, CAT_DRONE, CAT_FIGHTER, CAT_MODULE, CAT_SHIP, CAT_SUBSYSTEM, StaticData
from evesim.dogma import const as C
from evesim.fit import (DroneSpec, FighterSpec, Fit, ModuleSpec, can_activate, cap_active_drones, charge_fits,
                        default_state, fighter_class, is_launcher, is_turret, module_fits_ship, ship_modes, slot_of)
from evesim.optimize import pareto
from evesim.optimize.search import SHIP_CLASSES, Optimizer
from evesim.optimize.spec import METRICS, from_dict, _profile
from evesim.skills import profile as skillprof
from evesim.stats import compute

_data = None
_chars = {}


def _clean(o):
    if isinstance(o, float) and (math.isinf(o) or math.isnan(o)):
        return "inf" if o > 0 else None
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def init(path):
    global _data
    _data = StaticData.load(path)
    ships = sorted(t.name for t in _data.types.values() if t.category_id == CAT_SHIP and t.market_group)
    grades = sorted(g["name"] for g in _data.clone_grades.values())
    return json.dumps({"ships": ships, "grades": grades, "build": _data.meta.get("client_build"),
                       "classes": list(SHIP_CLASSES),
                       "metrics": {m.name: {"higher": m.higher_is_better, "unit": m.unit, "about": m.description}
                                   for m in METRICS.values()}})


def _profile_of(skills, skills_json=None):
    if skills == "file":
        return skillprof.from_dict(_data, json.loads(skills_json), name="uploaded")
    return skillprof.load(_data, skills)


def _char(skills, skills_json=None):
    if skills == "file":
        prof = _profile_of(skills, skills_json)
        return prof.character(_data), prof.name
    if skills not in _chars:
        prof = skillprof.load(_data, skills)
        _chars[skills] = (prof.character(_data), prof.name)
    return _chars[skills]


def _target(t):
    base = PRESETS[t.get("preset", "cruiser")]
    out = Target(base.name, base.signature, base.velocity, base.distance, base.transversal_share, dict(base.resonance))
    for k in ("signature", "velocity", "distance"):
        if t.get(k) not in (None, ""):
            setattr(out, k, float(t[k]))
    return out


def _stats_dict(st, target):
    d = st.to_dict()
    d["eft"] = st.fit.to_eft()
    d["offense"]["applied_dps"] = applied_dps(st, target)
    d["target"] = {"name": target.name, "signature": target.signature, "velocity": target.velocity,
                   "distance": target.distance}
    return d


def simulate(req_json):
    req = json.loads(req_json)
    fit = Fit.from_eft(req["eft"], _data)
    char, name = _char(req.get("skills", "all5"), req.get("skills_json"))
    target = _target(req.get("target", {}))
    st = compute(fit, char, profile=_profile(req.get("profile", "uniform")))
    d = _stats_dict(st, target)
    d["skills"] = name
    return json.dumps(_clean(d))


def optimize(req_json, log):
    req = json.loads(req_json)
    sd = req["spec"]
    if req.get("no_polarized"):
        pol = [t.name for t in _data.types.values() if t.name.startswith("Polarized ")]
        sd.setdefault("fitting", {}).setdefault("exclude", []).extend(pol)
    spec = from_dict(sd)
    char, name = _char(req.get("skills", "all5"), req.get("skills_json"))
    if spec.pareto:
        points = pareto.run(_data, char, spec, log=log)
        return json.dumps(_clean({"skills": name, "pareto": spec.pareto, "points": [
            {"ship": p.ship, "weights": p.weights, "metrics": {m: p.metrics[m] for m in spec.pareto},
             "stats": _stats_dict(p.evaluation.stats, spec.target)} for p in points]}))
    opt = Optimizer(_data, char, spec, log=log)
    results = opt.run()
    out = []
    for r in results:
        b = r.best
        item = {"ship": r.ship.name, "notes": r.notes, "evaluations": r.evaluations, "seconds": r.seconds}
        if b is not None:
            cons = []
            for c in spec.constraints:
                v = b.metrics[c.metric]
                ok = (v == math.inf) if c.bound == math.inf else c.shortfall(v) == 0
                cons.append({"metric": c.metric, "kind": c.kind, "bound": c.bound, "value": v, "ok": ok})
            item["best"] = {"feasible": b.feasible, "score": b.score, "metrics": b.metrics,
                            "constraints": cons, "stats": _stats_dict(b.stats, spec.target)}
            item["alternatives"] = [{
                "label": next((f"{m.type.name} / {m.charge.name}" for m in a.fit.modules
                               if m.charge and m.slot == "high"), "no weapons"),
                "score": a.score, "feasible": a.feasible,
                "metrics": {m: a.metrics[m] for m in spec.weights}} for a in r.alternatives]
        out.append(item)
    return json.dumps(_clean({"skills": name, "weights": spec.weights, "results": out,
                              "screened": opt.screened[:30]}))


# ---- fitting window ----------------------------------------------------------------------------

_KINDS = {CAT_CHARGE: "charge", CAT_DRONE: "drone", CAT_FIGHTER: "fighter"}
_META = {1: "t1", 2: "t2", 3: "storyline", 4: "faction", 5: "officer", 6: "deadspace", 14: "t3", 15: "abyssal"}


def _kind(t):
    if t.category_id in (CAT_MODULE, CAT_SUBSYSTEM):
        slot = slot_of(t)
        if slot is None:
            return None
        if slot == "high" and is_turret(t):
            return "high:turret"
        if slot == "high" and is_launcher(t):
            return "high:launcher"
        return slot
    if t.category_id == CAT_FIGHTER:
        return "fighter:" + fighter_class(t)
    return _KINDS.get(t.category_id)


def catalog():
    """Everything the fitting window lists, sent once: ships, fittable items, the market tree.

    Item rows are [id, name, kind, iconID, marketGroup, meta, flags, extra]; flags: a = can be
    activated, h = can be overheated, c = takes charges. extra is a fighter type's squadron
    size and a drone's bandwidth."""
    ships, items = [], []
    for t in _data.types.values():
        if not t.market_group:
            continue
        if t.category_id == CAT_SHIP:
            ships.append([t.id, t.name, t.group_name, t.graphic_id, t.market_group, t.race])
            continue
        kind = _kind(t)
        if kind is None:
            continue
        flags = ""
        if t.category_id == CAT_MODULE:
            if can_activate(t, _data):
                flags += "a"
            if any(e.category == C.EC_OVERLOAD for e in t.effects):
                flags += "h"
            if any(t.attrs.get(a) for a in (604, 605, 606, 609, 610)):
                flags += "c"
        extra = 0
        if t.category_id == CAT_FIGHTER:
            extra = int(t.attr("fighterSquadronMaxSize", 1.0))
        elif t.category_id == CAT_DRONE:
            extra = t.attr("droneBandwidthUsed", 0.0)
        items.append([t.id, t.name, kind, t.icon_id, t.market_group, _META.get(t.meta_group, ""), flags, extra])
    groups = {g: [n, parent, icon] for g, (n, parent, icon) in _data.market_groups.items()}
    return json.dumps({"ships": ships, "items": items, "market": groups})


_ship_cache = {}


def ship_info(req_json):
    """Slot layout of a hull, what can be fitted to it, and its tactical modes."""
    req = json.loads(req_json)
    ship = _data.types[int(req["ship"])]
    if ship.id not in _ship_cache:
        fits = [t.id for t in _data.types.values() if t.market_group and t.category_id in (CAT_MODULE, CAT_SUBSYSTEM)
                and slot_of(t) and module_fits_ship(t, ship, _data)]
        attr = lambda name: int(ship.attr(name, 0.0))
        _ship_cache[ship.id] = {
            "id": ship.id, "name": ship.name, "group": ship.group_name, "graphic": ship.graphic_id,
            "slots": {"high": attr("hiSlots"), "med": attr("medSlots"), "low": attr("lowSlots"),
                      "rig": attr("rigSlots"), "subsystem": attr("maxSubSystems")},
            "hardpoints": {"turret": attr("turretSlotsLeft"), "launcher": attr("launcherSlotsLeft")},
            "drones": ship.attr("droneCapacity", 0.0) > 0, "fighters": ship.attr("fighterTubes", 0.0) > 0,
            "modes": [[m.id, m.name.replace(ship.name + " ", "")] for m in ship_modes(ship, _data)],
            "fits": fits,
        }
    out = dict(_ship_cache[ship.id])
    char, _ = _char(req.get("skills", "all5"), req.get("skills_json"))
    out["unusable"] = [i for i in out["fits"] if not char.meets(_data.types[i])]
    return json.dumps(out)


def charges(req_json):
    """Charges that fit a module, grouped as the game does (by charge group)."""
    t = _data.types[int(json.loads(req_json)["module"])]
    groups = {int(t.attrs[a]) for a in (604, 605, 606, 609, 610) if t.attrs.get(a)}
    out = [c.id for c in _data.types.values()
           if c.category_id == CAT_CHARGE and c.market_group and c.group_id in groups and charge_fits(t, c)]
    return json.dumps(out)


def simulate_fit(req_json):
    """Stats for a fit given as type ids, plus per-module fitting costs for the slot tiles."""
    req = json.loads(req_json)
    T = _data.types
    ship = T[int(req["ship"])]
    fit = Fit(ship, name=req.get("name") or "Fit Lab")
    for m in req.get("modules", []):
        t = T[int(m["id"])]
        state = m.get("state")
        fit.modules.append(ModuleSpec(t, default_state(t, _data) if state is None else int(state),
                                      T[int(m["charge"])] if m.get("charge") else None))
    for d in req.get("drones", []):
        fit.drones.append(DroneSpec(T[int(d["id"])], int(d["count"]), int(d["count"])))
    for f in req.get("fighters", []):
        fit.fighters.append(FighterSpec(T[int(f["id"])], int(f["amount"]), bool(f.get("active", True))))
    cap_active_drones(fit)
    char, name = _char(req.get("skills", "all5"), req.get("skills_json"))
    target = _target(req.get("target", {}))
    st = compute(fit, char, profile=_profile(req.get("profile", "uniform")))
    d = _stats_dict(st, target)
    d["skills"] = name
    g = st.calc.get
    d["modules"] = [{"cpu": g(it, "cpu"), "power": g(it, "power"), "state": m.state,
                     "charge_qty": int(g(it, "capacity") // (m.charge.attrs.get(C.A_VOLUME) or 1.0)) if m.charge else 0}
                    for m, it in zip(fit.modules, st.calc.modules)]
    d["drones_active"] = [x.active for x in fit.drones]
    return json.dumps(_clean(d))


def parse_eft(req_json):
    """An EFT fit as type ids, for loading it into the fitting window."""
    fit = Fit.from_eft(json.loads(req_json)["eft"], _data)
    return json.dumps({"ship": fit.ship.id, "name": fit.name,
                       "modules": [{"id": m.type.id, "state": m.state, "charge": m.charge.id if m.charge else None,
                                    "slot": m.slot} for m in fit.modules],
                       "drones": [{"id": x.type.id, "count": x.count} for x in fit.drones],
                       "fighters": [{"id": x.type.id, "amount": x.amount} for x in fit.fighters]})


def train(req_json, log):
    from evesim.advisor import Advisor, objective_metrics
    req = json.loads(req_json)
    spec = from_dict(req["spec"])
    prof = _profile_of(req.get("skills", "all5"), req.get("skills_json"))
    attrs = dict(prof.attributes)
    attrs.update({k: float(v) for k, v in (req.get("attributes") or {}).items() if v not in (None, "")})
    fit = Fit.from_eft(req["eft"], _data)
    spec.ships = [fit.ship.name]
    adv = Advisor(_data, prof.levels, spec, prof.implants, attrs, log=log)
    days = float(req.get("days", 30))
    log("ranking single skills")
    single = adv.fit_suggestions(fit, days)[:15]
    log("building the training plan")
    plan, before, after = adv.plan(fit, days)
    refit, reference = [], None
    if req.get("refit"):
        log("re-optimizing for skills that unlock better modules")
        refit, _ = adv.refit_suggestions(days, int(req.get("unlocks", 5)))
        reference = adv.reference.metrics

    def sugg(s):
        return {"skill": s.skill, "from": s.from_level, "to": s.to_level, "seconds": s.seconds, "gain": s.gain,
                "per_day": s.gain_per_day, "steps": s.steps, "before": s.before, "after": s.after,
                "unlocks": s.unlocks, "eft": s.fit_eft}

    return json.dumps(_clean({"skills": prof.name, "attributes": adv.attributes, "metrics": objective_metrics(spec),
                              "single": [sugg(s) for s in single], "plan": [sugg(s) for s in plan],
                              "before": before, "after": after, "refit": [sugg(s) for s in refit],
                              "reference": reference, "ship": fit.ship.name}))
