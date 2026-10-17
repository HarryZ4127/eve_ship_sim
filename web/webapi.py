"""Bridge between the browser demo (web/index.html) and evesim, run under Pyodide."""

import json
import math

from evesim.application import PRESETS, Target, applied_dps
from evesim.data.store import CAT_SHIP, StaticData
from evesim.fit import Fit
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
