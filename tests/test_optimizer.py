"""End-to-end optimizer tests (quick effort, so they stay fast)."""

import math

from evesim.dogma import const as C
from evesim.fit import check
from evesim.optimize.search import Optimizer
from evesim.optimize.spec import from_dict
from evesim.skills.profile import all_level


def _run(data, char, d):
    spec = from_dict(d)
    return Optimizer(data, char, spec).run()[0], spec


def test_optimizer_respects_constraints(data, all5):
    res, spec = _run(data, all5, {
        "ship": "Rifter",
        "objective": {"maximize": "dps"},
        "constraints": {"min_ehp": 3000, "cap_stable": True},
        "fitting": {"propulsion": "ab"},
        "search": {"effort": "quick"},
    })
    best = res.best
    assert best.feasible, best.problems
    assert best.metrics["ehp"] >= 3000
    assert best.stats.cap.stable
    assert any(C.E_AB in m.type.effect_ids for m in best.fit.modules)
    assert not check(best.fit, all5)


def test_optimizer_only_uses_trained_skills(data):
    weak = all_level(data, 1).character(data)
    res, _ = _run(data, weak, {"ship": "Rifter", "objective": {"maximize": "dps"}, "search": {"effort": "quick"}})
    for m in res.best.fit.modules:
        assert weak.meets(m.type), m.type.name
        if m.charge:
            assert weak.meets(m.charge), m.charge.name


def test_optimizer_reports_infeasible(data, all5):
    res, _ = _run(data, all5, {
        "ship": "Rifter", "objective": {"maximize": "dps"},
        "constraints": {"min_ehp": 1_000_000}, "search": {"effort": "quick"},
    })
    assert not res.best.feasible
    assert res.best.metrics["ehp"] < 1_000_000


def test_required_and_excluded_modules(data, all5):
    res, _ = _run(data, all5, {
        "ship": "Rifter", "objective": {"maximize": "dps"},
        "fitting": {"require": ["Warp Scrambler II"], "exclude": ["Gyrostabilizer II"]},
        "search": {"effort": "quick"},
    })
    names = [m.type.name for m in res.best.fit.modules]
    assert "Warp Scrambler II" in names
    assert "Gyrostabilizer II" not in names


def test_unflyable_ship_is_reported(data):
    weak = all_level(data, 0).character(data)
    res, _ = _run(data, weak, {"ship": "Raven", "objective": {"maximize": "dps"}, "search": {"effort": "quick"}})
    assert res.best is None and "cannot fly" in res.notes[0]
    assert math.isfinite(len(res.notes))
