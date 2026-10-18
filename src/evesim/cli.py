"""Command line interface: ``evesim <command>``."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from evesim.data import build as data_build
from evesim.data.store import StaticData, default_data_path


def _data(args) -> StaticData:
    return StaticData.load(args.data)


def _prices(args, data):
    from evesim.market import Prices
    return Prices.load(data, offline=getattr(args, "offline", False))


def _skills(args, data):
    from evesim.skills.profile import load
    return load(data, args.skills)


def _target(args):
    from evesim.application import PRESETS, Target
    base = PRESETS[args.target]
    t = Target(base.name, base.signature, base.velocity, base.distance, base.transversal_share, dict(base.resonance))
    if args.distance is not None:
        t.distance = args.distance
    if args.target_velocity is not None:
        t.velocity = args.target_velocity
    if args.target_sig is not None:
        t.signature = args.target_sig
    return t


def _json_default(o):
    return str(o)


def _clean(o):
    """Make values strict-JSON safe (infinite cap time -> "inf")."""
    if isinstance(o, float) and (math.isinf(o) or math.isnan(o)):
        return "inf" if o > 0 else ("-inf" if o < 0 else None)
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def _dump(o) -> str:
    return json.dumps(_clean(o), indent=1, default=_json_default, allow_nan=False)


def cmd_data_update(args):
    out = Path(args.data) if args.data else default_data_path()
    print(f"building static data from {args.source or 'GitHub (pyfa-org/Pyfa ' + args.ref + ')'}")
    d = data_build.build(args.source, args.ref, log=lambda m: print("  " + m))
    data_build.write(d, out)
    print(f"wrote {out}: {len(d['types'])} types, client build {d['meta'].get('client_build')}")


def cmd_data_info(args):
    d = _data(args)
    print(json.dumps(d.meta, indent=1))
    print(f"{len(d.types)} types, {len(d.effects)} effects, {len(d.attributes)} attributes")


def cmd_find(args):
    d = _data(args)
    for t in d.find_types(args.text, limit=args.limit):
        print(f"{t.id:>7}  {t.name}  [{t.group_name}]")


def cmd_login(args):
    from evesim.skills import esi
    rec = esi.login(args.client_id, args.port, open_browser=not args.no_browser, save_fittings=args.save_fittings)
    print(f"logged in as {rec['name']} ({rec['character_id']})")


def cmd_characters(args):
    from evesim.skills import esi
    for c in esi.characters():
        print(f"{c['character_id']}  {c['name']}")


def cmd_skills(args):
    d = _data(args)
    prof = _skills(args, d)
    if args.output:
        prof.save(args.output, d)
        print(f"saved {len(prof.levels)} skills to {args.output}")
    else:
        print(json.dumps(prof.to_json(d), indent=1))


def cmd_stats(args):
    from evesim.fit import Fit
    from evesim.optimize.spec import _profile
    from evesim.report import stats_text
    from evesim.stats import compute
    d = _data(args)
    text = sys.stdin.read() if args.fit == "-" else Path(args.fit).read_text()
    fit = Fit.from_eft(text, d)
    char = _skills(args, d).character(d)
    st = compute(fit, char, profile=_profile(args.profile), assume_nos=args.assume_nos)
    st.price = _prices(args, d).fit_price(fit)
    target = _target(args)
    if args.json:
        from evesim.application import applied_dps
        out = st.to_dict()
        out["offense"]["applied_dps"] = applied_dps(st, target)
        print(_dump(out))
    else:
        print(stats_text(st, target))


def cmd_optimize(args):
    from evesim.optimize import spec as specmod
    from evesim.optimize.search import Optimizer
    from evesim.report import result_text
    d = _data(args)
    sp = specmod.load(args.spec)
    if args.ship:
        sp.ships, sp.ship_groups = list(args.ship), []
    if args.effort:
        sp.effort = args.effort
    prof = _skills(args, d)
    char = prof.character(d)
    prices = _prices(args, d)
    if prices.source == "basePrice" and any(c.metric == "price" for c in sp.constraints):
        print("warning: no market prices available (ESI unreachable); budget uses static base prices",
              file=sys.stderr)
    log = (lambda m: print(m, file=sys.stderr)) if not args.quiet else (lambda m: None)
    if sp.pareto:
        from evesim.optimize import pareto
        from evesim.report import pareto_text
        points = pareto.run(d, char, sp, prices, log=log)
        if args.json:
            print(_dump([{"ship": p.ship, "weights": p.weights, "metrics": p.metrics, "eft": p.evaluation.fit.to_eft()}
                         for p in points]))
        else:
            print(f"Skills: {prof.name} ({prof.source})")
            print(pareto_text(points, sp))
        return
    opt = Optimizer(d, char, sp, prices, log=log)
    results = opt.run()
    if args.json:
        out = []
        for r in results:
            b = r.best
            out.append({"ship": r.ship.name, "notes": r.notes,
                        "best": None if b is None else {
                            "feasible": b.feasible, "score": b.score, "metrics": b.metrics,
                            "eft": b.fit.to_eft(), "stats": b.stats.to_dict()}})
        print(_dump(out))
    else:
        print(f"Skills: {prof.name} ({prof.source})")
        print(result_text(results, sp))
    if args.eft_out and results and results[0].best:
        Path(args.eft_out).write_text(results[0].best.fit.to_eft())


def _parse_weights(text: str) -> dict[str, float]:
    out = {}
    for part in text.split(","):
        name, _, w = part.partition("=")
        out[name.strip()] = float(w) if w.strip() else 1.0
    return out


def cmd_train(args):
    from evesim.advisor import Advisor
    from evesim.fit import Fit
    from evesim.optimize import spec as specmod
    from evesim.report import training_text
    d = _data(args)
    if args.spec:
        sp = specmod.load(args.spec)
    else:
        sp = specmod.from_dict({"objective": {"weights": _parse_weights(args.objective)},
                                "target": {"preset": args.target, **({"distance": args.distance} if args.distance else {})},
                                "incoming": {"preset": args.profile}})
    if args.effort:
        sp.effort = args.effort
    prof = _skills(args, d)
    attrs = dict(prof.attributes)
    if args.attributes:
        attrs.update({k: float(v) for k, v in (kv.split("=") for kv in args.attributes.split(","))})
    log = (lambda m: print(m, file=sys.stderr)) if not args.quiet else (lambda m: None)
    adv = Advisor(d, prof.levels, sp, prof.implants, attrs, _prices(args, d), log=log)
    fit = None
    if args.fit:
        text = sys.stdin.read() if args.fit == "-" else Path(args.fit).read_text()
        fit = Fit.from_eft(text, d)
        sp.ships = [fit.ship.name]
    refit, base = [], None
    if args.refit:
        if not sp.ships:
            raise ValueError("--refit needs a ship: give a fit or a spec with ship = ...")
        refit, base = adv.refit_suggestions(args.days, args.unlocks)
        if fit is None:
            fit = base.best.fit
    if fit is None:
        raise ValueError("give a fit (EFT file) to analyse, or --refit with a spec")
    single = adv.fit_suggestions(fit, args.days)
    plan, before, after = adv.plan(fit, args.days)
    print(training_text(prof, adv, sp, fit, single[:args.top], plan, before, after, refit, args.days))


def cmd_metrics(args):
    from evesim.optimize.spec import METRICS
    for m in METRICS.values():
        print(f"{m.name:<20} {'max' if m.higher_is_better else 'min'}  {m.unit:<6} {m.description}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="evesim", description="EVE Online ship simulator and fit optimizer")
    p.add_argument("--data", help="static data file (default: %s)" % default_data_path())
    sub = p.add_subparsers(dest="cmd", required=True)

    def skills_arg(sp):
        sp.add_argument("--skills", "-s", default="all5",
                        help="all0..all5, 'alpha <race>', esi:<character>, or a JSON file (default all5)")

    sp = sub.add_parser("data", help="manage static data")
    dsub = sp.add_subparsers(dest="data_cmd", required=True)
    u = dsub.add_parser("update", help="download/build static data")
    u.add_argument("--source", help="local Pyfa checkout or staticdata dir instead of downloading")
    u.add_argument("--ref", default="master", help="Pyfa git ref to download (default master)")
    u.set_defaults(fn=cmd_data_update)
    i = dsub.add_parser("info", help="show static data version")
    i.set_defaults(fn=cmd_data_info)

    f = sub.add_parser("find", help="search item names")
    f.add_argument("text")
    f.add_argument("--limit", type=int, default=20)
    f.set_defaults(fn=cmd_find)

    lg = sub.add_parser("login", help="log a character in through EVE SSO")
    lg.add_argument("--client-id", help="SSO application client ID (or EVESIM_CLIENT_ID)")
    lg.add_argument("--port", type=int, default=8642)
    lg.add_argument("--no-browser", action="store_true")
    lg.add_argument("--save-fittings", action="store_true",
                    help="also request esi-fittings.write_fittings.v1 (enable it on your application first)")
    lg.set_defaults(fn=cmd_login)
    ch = sub.add_parser("characters", help="list logged-in characters")
    ch.set_defaults(fn=cmd_characters)

    sk = sub.add_parser("skills", help="show or save a skill profile")
    skills_arg(sk)
    sk.add_argument("--output", "-o", help="write profile JSON here")
    sk.set_defaults(fn=cmd_skills)

    st = sub.add_parser("stats", help="simulate a fit (EFT text file, or - for stdin)")
    st.add_argument("fit")
    skills_arg(st)
    st.add_argument("--profile", default="uniform", help="incoming damage profile (uniform, guristas, ...)")
    st.add_argument("--target", default="cruiser", help="target preset for applied DPS")
    st.add_argument("--distance", type=float)
    st.add_argument("--target-velocity", type=float)
    st.add_argument("--target-sig", type=float)
    st.add_argument("--assume-nos", action="store_true", help="count Nosferatu as cap gain")
    st.add_argument("--offline", action="store_true", help="do not fetch market prices")
    st.add_argument("--json", action="store_true")
    st.set_defaults(fn=cmd_stats)

    op = sub.add_parser("optimize", help="find the best fit for a requirements spec (TOML)")
    op.add_argument("spec")
    skills_arg(op)
    op.add_argument("--ship", action="append", help="override the spec's ship (repeatable)")
    op.add_argument("--effort", choices=["quick", "normal", "thorough"])
    op.add_argument("--offline", action="store_true", help="do not fetch market prices")
    op.add_argument("--eft-out", help="also write the best fit as EFT text")
    op.add_argument("--json", action="store_true")
    op.add_argument("--quiet", "-q", action="store_true")
    op.set_defaults(fn=cmd_optimize)

    tr = sub.add_parser("train", help="skill training advice: which skills improve a fit most per day")
    tr.add_argument("fit", nargs="?", help="EFT fit to analyse (- for stdin); optional with --refit and --spec")
    skills_arg(tr)
    tr.add_argument("--spec", help="requirements spec: objective, target and constraints to value skills by")
    tr.add_argument("--objective", default="dps=1,ehp=1", help="weights when no spec is given (default dps=1,ehp=1)")
    tr.add_argument("--target", default="cruiser")
    tr.add_argument("--distance", type=float)
    tr.add_argument("--profile", default="uniform")
    tr.add_argument("--days", type=float, default=30, help="training-time budget for the plan (default 30)")
    tr.add_argument("--top", type=int, default=15, help="how many single-skill suggestions to list")
    tr.add_argument("--refit", action="store_true", help="also value skills that unlock better modules (re-optimizes)")
    tr.add_argument("--unlocks", type=int, default=8, help="how many unlock candidates to re-optimize for")
    tr.add_argument("--attributes", help="training attributes, e.g. perception=27,willpower=21 (default 20 each, "
                                         "or from ESI)")
    tr.add_argument("--effort", choices=["quick", "normal", "thorough"])
    tr.add_argument("--offline", action="store_true")
    tr.add_argument("--quiet", "-q", action="store_true")
    tr.set_defaults(fn=cmd_train)

    me = sub.add_parser("metrics", help="list metrics usable in objectives and constraints")
    me.set_defaults(fn=cmd_metrics)

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except (FileNotFoundError, KeyError, ValueError, RuntimeError) as e:
        msg = e.args[0] if isinstance(e, KeyError) and e.args else e
        print(f"error: {msg}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
