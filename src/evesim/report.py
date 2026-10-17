"""Human-readable rendering of stats and optimization results."""

from __future__ import annotations

import math

from evesim.application import Target, applied_dps
from evesim.dogma import const as C
from evesim.optimize.search import Result
from evesim.optimize.spec import METRICS, Spec
from evesim.stats import Stats


def _n(v: float, d: int = 0) -> str:
    if v == math.inf:
        return "inf"
    return f"{v:,.{d}f}"


def stats_text(st: Stats, target: Target | None = None) -> str:
    L = []
    L.append(f"== {st.fit.ship.name}: {st.fit.name}")
    if st.problems:
        L.append("!! " + "\n!! ".join(st.problems))
    r = st.resources
    L.append(f"Fitting   CPU {_n(r['cpu'][0], 1)}/{_n(r['cpu'][1], 1)}  PG {_n(r['power'][0], 1)}/{_n(r['power'][1], 1)}"
             f"  Calibration {_n(r['calibration'][0])}/{_n(r['calibration'][1])}")
    L.append("Slots     " + "  ".join(f"{k} {u}/{t}" for k, (u, t) in st.slots.items())
             + "  | turrets {}/{}  launchers {}/{}".format(*st.hardpoints["turret"], *st.hardpoints["launcher"]))
    fighters = f", fighters {_n(st.fighter_dps, 1)}" if st.fighter_dps else ""
    L.append(f"Offense   DPS {_n(st.dps, 1)} (weapons {_n(st.weapon_dps, 1)}, drones {_n(st.drone_dps, 1)}{fighters})"
             f"  volley {_n(st.volley)}")
    if target is not None:
        L.append(f"          applied vs {target.name} (sig {target.signature:g} m, {target.velocity:g} m/s, "
                 f"{target.distance / 1000:g} km): {_n(applied_dps(st, target), 1)}")
    for w in st.weapons:
        L.append(f"   {w.count}x {w.name}" + (f" [{w.charge}]" if w.charge else "")
                 + f": {_n(w.dps(), 1)} dps, {w.range_summary()}")
    L.append(f"Defense   EHP {_n(st.ehp_total)} (shield {_n(st.ehp['shield'])}, armor {_n(st.ehp['armor'])}, "
             f"hull {_n(st.ehp['hull'])})  profile " + "/".join(f"{st.profile[k] * 100:.0f}" for k in C.DAMAGE_TYPES))
    for layer in ("shield", "armor", "hull"):
        res = st.resonance[layer]
        L.append(f"   {layer:<6} {_n(st.hp[layer]):>8} HP  resists " +
                 " ".join(f"{k[:2]} {100 * (1 - res[k]):4.1f}%" for k in C.DAMAGE_TYPES))
    reload = ""
    if abs(st.sustained_ehps_reload - st.sustained_ehps) > 0.05:
        reload = f" ({_n(st.sustained_ehps_reload, 1)} with ancillary reloads)"
    L.append(f"   tank sustained {_n(st.sustained_ehps, 1)} EHP/s{reload}, peak {_n(st.peak_ehps, 1)} EHP/s")
    cap = st.cap
    state = f"stable at {cap.stable_fraction * 100:.1f}%" if cap.stable else f"lasts {cap.lasts_s:.0f} s"
    L.append(f"Capacitor {_n(st.cap_capacity)} GJ, {st.cap_recharge_s:.0f} s recharge, {state}; "
             f"use {cap.use_per_s:.1f} GJ/s vs peak recharge {cap.peak_recharge:.1f}"
             + (f" + boosters {cap.inject_per_s:.1f}" if cap.inject_per_s else ""))
    n = st.nav
    L.append(f"Movement  {_n(n['max_velocity'], 1)} m/s, align {n['align_time']:.2f} s, sig {_n(n['signature_radius'], 1)} m,"
             f" warp {n['warp_speed']:.1f} AU/s")
    t = st.targeting
    L.append(f"Targeting {t['lock_range'] / 1000:.1f} km, scan res {_n(t['scan_resolution'])} mm,"
             f" {int(t['max_targets'])} targets")
    d = st.drones
    if d["bandwidth"]:
        L.append(f"Drones    bandwidth {_n(d['bandwidth_used'])}/{_n(d['bandwidth'])}, bay {_n(d['bay_used'])}/{_n(d['bay'])}")
    if d.get("fighter_tubes"):
        L.append(f"Fighters  tubes {int(d['fighter_tubes_used'])}/{int(d['fighter_tubes'])}, "
                 f"bay {_n(d['fighter_bay_used'])}/{_n(d['fighter_bay'])} m3")
    for e in st.utility.get("ewar", []):
        what = f"{_n(e['per_second'], 1)} GJ/s" if e["kind"] in ("neut", "nos") else f"{_n(e['strength'], 1)} {e['unit']}"
        rng = f"{e['optimal'] / 1000:.1f} km" + (f" + {e['falloff'] / 1000:.1f} km" if e["falloff"] else "")
        L.append(f"EWAR      {e['count']}x {e['name']}: {what}, {rng}")
    if st.utility.get("remote_repair") and any(st.utility["remote_repair"].values()):
        L.append("Remote    " + ", ".join(f"{k} {_n(v, 1)} HP/s" for k, v in st.utility["remote_repair"].items() if v))
    u = st.utility
    mined = [f"{k} {_n(u[k + '_yield'] if k != 'ore' else u['mining_yield'], 2)} m3/s"
             for k in ("ore", "ice", "gas") if u.get("mining_yield" if k == "ore" else k + "_yield")]
    if mined:
        L.append("Mining    " + ", ".join(mined) + (f" (residue {_n(u['mining_waste'], 2)} m3/s)" if u.get("mining_waste") else ""))
    for p in u.get("probes", []):
        L.append(f"Probes    {p['name']}: strength {_n(p['strength'], 1)}, deviation {p['deviation']:.3f} AU"
                 + (" (finds ships)" if p["ships"] else ""))
    for kind, h in u.get("hacking", {}).items():
        L.append(f"Hacking   {kind} ({h['name']}): coherence {_n(h['coherence'])}, strength {_n(h['strength'])},"
                 f" {h['slots']} utility slots")
    if u.get("salvage_chance"):
        L.append(f"Salvage   +{_n(u['salvage_chance'])}% salvage chance")
    if st.price is not None:
        L.append(f"Price     {_n(st.price)} ISK")
    return "\n".join(L)


def result_text(results: list[Result], spec: Spec) -> str:
    L = []
    for res in results:
        for note in res.notes:
            L.append(f"-- {note}")
        b = res.best
        if b is None:
            continue
        L.append(f"## {res.ship.name}: {'FEASIBLE' if b.feasible else 'INFEASIBLE (best effort)'}"
                 f"  score {b.score:.3f}  ({res.evaluations} fits evaluated in {res.seconds:.1f} s)")
        L.append("Objective: " + " + ".join(f"{w:g} x {m}" for m, w in spec.weights.items()))
        for m in spec.weights:
            L.append(f"   {m:<16} {_n(b.metrics[m], 1)} {METRICS[m].unit}")
        if spec.constraints:
            L.append("Constraints:")
            for c in spec.constraints:
                v = b.metrics[c.metric]
                ok = (v == math.inf) if c.bound == math.inf else (c.shortfall(v) == 0)
                bound = "stable" if c.bound == math.inf else f"{'>=' if c.kind == 'min' else '<='} {c.bound:g}"
                tight = ""
                if ok and c.bound not in (0, math.inf) and abs(v - c.bound) <= 0.02 * abs(c.bound):
                    tight = "  (binding)"
                L.append(f"   [{'ok' if ok else 'XX'}] {c.metric:<14} {_n(v, 1):>12} {bound}{tight}")
        L.append("")
        L.append(b.fit.to_eft().rstrip())
        L.append("")
        L.append(stats_text(b.stats, spec.target))
        if res.alternatives:
            L.append("")
            L.append("Other weapon setups tried:")
            for a in res.alternatives:
                w = next((m for m in a.fit.modules if m.charge and m.slot == "high"), None)
                label = f"{w.type.name} / {w.charge.name}" if w else "no weapons"
                L.append(f"   {label}: score {a.score:.3f}, " +
                         ", ".join(f"{m} {_n(a.metrics[m], 1)}" for m in spec.weights)
                         + ("" if a.feasible else "  [infeasible]"))
        L.append("")
    return "\n".join(L).rstrip() + "\n"


def pareto_text(points, spec: Spec) -> str:
    ms = spec.pareto
    L = [f"Pareto front over {', '.join(ms)}: {len(points)} fits, none beaten on all of them by another", ""]
    head = f"{'#':>2}  {'ship':<20}" + "".join(f"{m:>16}" for m in ms)
    L += [head, "-" * len(head)]
    for i, p in enumerate(points, 1):
        L.append(f"{i:>2}  {p.ship:<20}" + "".join(f"{_n(p.metrics[m], 1):>16}" for m in ms))
    for i, p in enumerate(points, 1):
        L += ["", f"--- #{i} ({', '.join(f'{m} {w:.2f}' for m, w in p.weights.items())})", p.evaluation.fit.to_eft().rstrip()]
    return "\n".join(L) + "\n"


def _dur(seconds: float) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d {h}h" if d else (f"{h}h {m}m" if h else f"{m}m")


def training_text(prof, adv, spec: Spec, fit, single, plan, before, after, refit, days) -> str:
    from evesim.advisor import describe_change, objective_metrics
    roman = {0: "0", 1: "I", 2: "II", 3: "III", 4: "IV", 5: "V"}
    attrs = ", ".join(f"{k} {v:g}" for k, v in adv.attributes.items())
    L = [f"Skills: {prof.name} ({prof.source})   attributes: {attrs}",
         "Objective: " + " + ".join(f"{w:g} x {m}" for m, w in spec.weights.items()),
         f"Fit analysed: {fit.ship.name} ({fit.name})", ""]
    L.append("Best single trainings for this fit (gain per day of training):")
    if not single:
        L.append("   none: no skill raises the objective for this fit")
    for s in single:
        pre = [f"{n} {roman[b]}" for n, a, b in s.steps[:-1]]
        L.append(f"   {s.skill} {roman[s.from_level]} -> {roman[s.to_level]:<4} {_dur(s.seconds):>8}   "
                 f"{describe_change(s, spec)}" + (f"   (needs {', '.join(pre)})" if pre else ""))
    L += ["", f"Training plan, {days:g} days:"]
    total = 0.0
    for s in plan:
        total += s.seconds
        L.append(f"   {_dur(total):>8}  {s.skill} {roman[s.to_level]}")
    if plan:
        changes = []
        for m in objective_metrics(spec):
            a, b = before.get(m), after.get(m)
            if a is not None and b is not None and a != b and abs(a) != float("inf") and abs(b) != float("inf"):
                changes.append(f"{m} {a:,.1f} -> {b:,.1f}" + (f" ({(b - a) / abs(a) * 100:+.1f}%)" if a else ""))
        L.append("   result: " + ("; ".join(changes) or "no change"))
    else:
        L.append("   nothing to train within the budget improves this fit")
    if refit or getattr(adv, "reference", None) is not None:
        ref = adv.reference
        L += ["", "Skills that unlock a better fit (the fit is re-optimized with the skill trained), compared "
                  "with the best fit your current skills can fly (" + ", ".join(
                      f"{m} {_n(ref.metrics[m], 1)}" for m in spec.weights) + "):"]
        if not refit:
            L.append("   none found within the budget")
        for s in refit:
            L.append(f"   {s.skill} {roman[s.to_level]:<4} {_dur(s.seconds):>8}   {describe_change(s, spec)}")
            L.append(f"        unlocks e.g. {', '.join(s.unlocks[:4])}")
    return "\n".join(L) + "\n"
