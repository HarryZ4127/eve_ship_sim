"""Audit how well evesim covers every ship hull.

    python tools/audit_ships.py [--optimize] [--json out.json]

For each published hull: compute stats on the empty hull with all-V skills, list hull
effects the engine cannot execute (no modifierInfo and no hand-written handler), and
flag features evesim does not model (fighters, T3 destroyer modes, subsystems).
With --optimize, also run a quick optimization for one hull per ship group.
"""

import argparse
import collections
import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evesim.data.store import CAT_SHIP, StaticData  # noqa: E402
from evesim.dogma import engine  # noqa: E402
from evesim.fit import Fit  # noqa: E402
from evesim.optimize.search import Optimizer  # noqa: E402
from evesim.optimize.spec import from_dict  # noqa: E402
from evesim.skills.profile import all_level  # noqa: E402
from evesim.stats import compute  # noqa: E402

# Hull effects without modifierInfo that are harmless for fitting stats.
IGNORABLE = {"online", "hiPower", "medPower", "loPower", "rigSlot", "subSystem", "turretFitted", "launcherFitted"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--optimize", action="store_true")
    ap.add_argument("--json")
    a = ap.parse_args()
    data = StaticData.load()
    char = all_level(data, 5).character(data)
    ships = sorted((t for t in data.types.values() if t.category_id == CAT_SHIP and t.market_group),
                   key=lambda t: (t.group_name, t.name))
    rows = []
    unsupported = collections.Counter()
    for t in ships:
        row = {"ship": t.name, "group": t.group_name, "flags": [], "error": None}
        eff = [e for e in t.effects if not e.modifiers and e.name not in IGNORABLE and e.id not in engine._SPECIAL]
        for e in eff:
            unsupported[e.name] += 1
        if eff:
            row["flags"].append("hull effects not executed: " + ", ".join(e.name for e in eff))
        if t.attr("fighterCapacity", 0.0):
            row["flags"].append("fighters not modelled")
        if t.attr("maxSubSystems", 0.0):
            row["flags"].append("T3 cruiser: subsystems must be listed")
        if any(m.group_id == 1306 and int(m.attr("fitsToShipType", 0.0)) == t.id for m in data.types.values()):
            row["flags"].append("has tactical modes")
        try:
            st = compute(Fit(t), char)
            row["ehp"] = round(st.ehp_total)
            row["speed"] = round(st.nav["max_velocity"], 1)
        except Exception as e:  # noqa: BLE001
            row["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        rows.append(row)

    by_group = collections.defaultdict(list)
    for r in rows:
        by_group[r["group"]].append(r)
    print(f"{len(rows)} hulls, {sum(1 for r in rows if r['error'])} errors")
    for g, rs in sorted(by_group.items()):
        flags = collections.Counter(f for r in rs for f in r["flags"])
        print(f"  {g:<28} {len(rs):>3}  " + "; ".join(f"{k} ({v})" for k, v in flags.most_common()))
    print("Hull effects the engine does not execute:")
    for name, n in unsupported.most_common():
        print(f"  {n:>3}  {name}")

    if a.optimize:
        print("\nQuick optimization, one hull per group:")
        for g, rs in sorted(by_group.items()):
            ship = rs[0]["ship"]
            spec = from_dict({"ship": ship, "objective": {"weights": {"dps": 1.0, "ehp": 1.0}},
                              "search": {"effort": "quick"}})
            t0 = time.time()
            try:
                res = Optimizer(data, char, spec).run()[0]
                b = res.best
                msg = (f"dps {b.metrics['dps']:.0f} ehp {b.metrics['ehp']:.0f} "
                       f"{'ok' if b.feasible else 'INFEASIBLE ' + '; '.join(b.problems)}") if b else "; ".join(res.notes)
            except Exception as e:  # noqa: BLE001
                msg = f"ERROR {type(e).__name__}: {e}"
                traceback.print_exc()
            line = f"  {g:<28} {ship:<34} {time.time() - t0:5.1f}s  {msg}"
            print(line, flush=True)
            for r in rs:
                if r["ship"] == ship:
                    r["optimize"] = msg
    if a.json:
        Path(a.json).write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
