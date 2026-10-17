"""Tactical modes, utility metrics, many-hull search, Pareto fronts and the training advisor."""

import math

import pytest

from evesim.advisor import Advisor, sp_for_level, steps_to, train_seconds
from evesim.fit import Fit, check, ship_modes
from evesim.optimize import pareto
from evesim.optimize.search import GROUPS_STRUCTURE_FIGHTERS, Optimizer
from evesim.optimize.spec import from_dict
from evesim.skills.profile import all_level
from evesim.stats import compute


def test_sp_table():
    assert [sp_for_level(1, lvl) for lvl in range(6)] == [0, 250, 1414, 8000, 45255, 256000]
    assert sp_for_level(3, 5) == 768000


def test_training_time(data):
    hm = data.type_by_name("Heavy Missiles")  # rank 3, perception/willpower
    secs = train_seconds(hm, 0, 1, {"perception": 27, "willpower": 21})
    assert secs == pytest.approx(750 / (27 + 10.5) * 60)


def test_prerequisite_steps(data):
    hm = data.type_by_name("Heavy Missiles")
    steps = steps_to(data, {}, hm.id, 1)
    names = [data.types[s].name for s, _, _ in steps]
    assert names[-1] == "Heavy Missiles"
    assert "Missile Launcher Operation" in names and names.index("Missile Launcher Operation") < len(names) - 1


def test_tactical_modes_apply_and_are_required(data, all5):
    svipul = data.type_by_name("Svipul")
    modes = ship_modes(svipul, data)
    assert len(modes) == 3
    bare = Fit.from_eft("[Svipul, x]\n200mm AutoCannon II, Republic Fleet EMP S\n", data)
    assert any("tactical mode" in p for p in check(bare, all5))
    sharp = Fit.from_eft("[Svipul, x]\n200mm AutoCannon II, Republic Fleet EMP S\nSvipul Sharpshooter Mode\n", data)
    assert compute(sharp, all5).dps > compute(bare, all5).dps
    assert not check(sharp, all5)


def test_optimizer_picks_a_mode(data, all5):
    spec = from_dict({"ship": "Svipul", "objective": {"maximize": "dps"}, "search": {"effort": "quick"}})
    best = Optimizer(data, all5, spec).run()[0].best
    assert sum(1 for m in best.fit.modules if m.slot == "mode") == 1
    assert not check(best.fit, all5)


def test_burst_modules_default_off(data, all5):
    fit = Fit.from_eft("[Rifter, x]\nAssault Damage Control II\n", data)
    assert fit.modules[0].state == 1  # online, not active


@pytest.mark.parametrize("ship, metric", [("Hulk", "mining_yield"), ("Scimitar", "remote_repair"),
                                          ("Badger", "cargo"), ("Skiff", "ice_yield"), ("Venture", "gas_yield"),
                                          ("Astero", "probe_strength"), ("Heron", "relic_coherence"),
                                          ("Noctis", "salvage_chance")])
def test_role_metrics_drive_the_optimizer(data, all5, ship, metric):
    spec = from_dict({"ship": ship, "objective": {"maximize": metric}, "search": {"effort": "quick"}})
    best = Optimizer(data, all5, spec).run()[0].best
    empty = Optimizer(data, all5, spec).evaluate(Fit(data.type_by_name(ship)))
    assert best.metrics[metric] > empty.metrics[metric]
    assert not any("needs" in p for p in check(best.fit, all5))


def test_survival_time_needs_incoming_dps():
    with pytest.raises(ValueError):
        from_dict({"ship": "Drake", "objective": {"maximize": "survival_time"}})
    sp = from_dict({"ship": "Drake", "objective": {"maximize": "survival_time"},
                    "incoming": {"preset": "guristas", "dps": 300}})
    assert sp.incoming_dps == 300


def test_ship_class_screening(data, all5):
    spec = from_dict({"ship_classes": ["destroyer"], "objective": {"maximize": "dps"},
                      "search": {"effort": "quick", "screen_top": 2}})
    opt = Optimizer(data, all5, spec)
    results = opt.run()
    assert len(opt.screened) > 5 and len(results) == 2
    assert results[0].best.score >= results[1].best.score


def test_pareto_front_is_non_dominated(data, all5):
    spec = from_dict({"ship": "Rifter", "objective": {"pareto": ["dps", "ehp"], "points": 4},
                      "search": {"effort": "quick"}})
    pts = pareto.run(data, all5, spec)
    assert len(pts) >= 2
    for a in pts:
        for b in pts:
            assert not pareto.dominates(a.metrics, b.metrics, ["dps", "ehp"]) or a is b


def test_weight_vectors():
    vs = pareto.weight_vectors(3, 10)
    assert len(vs) >= 10 and all(math.isclose(sum(v), 1.0) for v in vs)
    assert (1.0, 0.0, 0.0) in vs and (0.0, 0.0, 1.0) in vs


def test_advisor_ranks_relevant_skills(data):
    levels = {k: min(v, 3) for k, v in all_level(data, 5).levels.items()}
    spec = from_dict({"objective": {"maximize": "dps"}})
    fit = Fit.from_eft("[Rifter, x]\n200mm AutoCannon I, EMP S\n200mm AutoCannon I, EMP S\n", data)
    adv = Advisor(data, levels, spec)
    sugg = adv.fit_suggestions(fit, max_days=30)
    names = {s.skill for s in sugg}
    assert "Small Projectile Turret" in names and "Surgical Strike" in names
    assert all(s.gain > 0 for s in sugg)
    assert "Mechanics" not in names  # tank skills do not raise DPS
    plan, before, after = adv.plan(fit, days=10)
    assert plan and after["dps"] > before["dps"]
    assert sum(s.seconds for s in plan) <= 10 * 86400


def test_ewar_stats(data, all5):
    fit = Fit.from_eft("[Curse, x]\nMedium Energy Neutralizer II\nMedium Energy Neutralizer II\n\n"
                       "Warp Disruptor II\nStasis Webifier II\n", data)
    st = compute(fit, all5)
    kinds = {e["kind"]: e for e in st.utility["ewar"]}
    assert kinds["neut"]["count"] == 2 and kinds["neut"]["per_second"] > 0
    assert kinds["point"]["optimal"] >= 24000  # skills and hull can only extend it
    assert kinds["web"]["strength"] == pytest.approx(60.0)


def test_ewar_constraints_make_the_optimizer_fit_tackle(data, all5):
    spec = from_dict({"ship": "Rifter", "objective": {"maximize": "dps"},
                      "constraints": {"min_web_strength": 50, "min_tackle_range": 9000},
                      "search": {"effort": "quick"}})
    best = Optimizer(data, all5, spec).run()[0].best
    assert best.feasible
    assert best.metrics["web_strength"] >= 50 and best.metrics["tackle_range"] >= 9000


THANATOS = ("[Thanatos, fighters]\nFighter Support Unit II\nFighter Support Unit II\n\n\n\n"
            "Templar II x6\nTemplar II x6\nTemplar II x6\nSiren II x3\n")


def test_fighters_round_trip_and_check(data, all5):
    fit = Fit.from_eft(THANATOS, data)
    assert [(f.type.name, f.amount) for f in fit.fighters] == [("Templar II", 6)] * 3 + [("Siren II", 3)]
    assert check(fit, all5) == []
    assert "Templar II x6" in fit.to_eft()
    st = compute(fit, all5)
    assert st.fighter_dps > 0 and st.weapon_dps == 0
    assert st.dps == pytest.approx(st.drone_dps + st.fighter_dps)


def test_fighter_tube_and_bay_limits(data, all5):
    too_many = Fit.from_eft("[Thanatos, x]\n\n\n\n\n" + "Templar II x6\n" * 4, data)
    assert any("light" in p or "tube" in p for p in check(too_many, all5))
    oversized = Fit.from_eft("[Thanatos, x]\n\n\n\n\nTemplar II x12\n", data)
    assert check(oversized, all5)


def test_optimizer_picks_fighters(data, all5):
    spec = from_dict({"ship": "Thanatos", "objective": {"maximize": "dps"},
                      "search": {"effort": "quick"}})
    best = Optimizer(data, all5, spec).run()[0].best
    assert best.fit.fighters and compute(best.fit, all5).fighter_dps > 0
    assert not any(f.type.group_id in GROUPS_STRUCTURE_FIGHTERS for f in best.fit.fighters)
    assert check(best.fit, all5) == []


def test_exploration_stats(data, all5):
    fit = Fit.from_eft("[Heron, x]\n\n\nZeugma Integrated Analyzer\n\n"
                       "Expanded Probe Launcher I, Combat Scanner Probe I\n", data)
    u = compute(fit, all5).utility
    assert u["hacking"]["data"] == u["hacking"]["relic"]  # integrated analyzers do both
    assert u["probes"][0]["ships"] and u["probes"][0]["strength"] > 20  # skills and hull add to it
    core = Fit.from_eft("[Heron, x]\n\n\n\nCore Probe Launcher I, Combat Scanner Probe I\n", data)
    assert any("cannot be loaded" in p for p in check(core, all5))


def test_harvest_kinds(data, all5):
    fit = Fit.from_eft("[Venture, x]\n\n\nGas Cloud Scoop I\nMiner I\n", data)
    u = compute(fit, all5).utility
    assert u["gas_yield"] > 0 and u["mining_yield"] > 0 and u["ice_yield"] == 0


def test_combined_hacking_needs_both_kinds(data, all5):
    integrated = [t.name for t in data.types.values() if t.name.endswith("Integrated Analyzer")]
    spec = from_dict({"ship": "Heron", "objective": {"maximize": "hacking_power"},
                      "fitting": {"exclude": integrated}, "search": {"effort": "quick"}})
    best = Optimizer(data, all5, spec).run()[0].best
    names = [m.type.name for m in best.fit.modules]
    assert any(n.startswith("Data Analyzer") for n in names) and any(n.startswith("Relic Analyzer") for n in names)
    m = best.metrics
    assert m["hacking_power"] == min(m["data_coherence"] * m["data_strength"], m["relic_coherence"] * m["relic_strength"])
