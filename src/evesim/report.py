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
    L.append(f"Offense   DPS {_n(st.dps, 1)} (weapons {_n(st.weapon_dps, 1)}, drones {_n(st.drone_dps, 1)})"
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
