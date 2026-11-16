"""Real-fit corpus parsing and the fit prior trained on it."""

import math

import pytest

from evesim import corpus, prior
from evesim.fit import Fit
from evesim.optimize.search import Optimizer
from evesim.optimize.spec import from_dict


def _id(data, name):
    return data.type_by_name(name).id


def test_killmail_victim_fit_and_tag(data):
    km = {"killmail_id": 7, "victim": {"ship_type_id": _id(data, "Caracal"), "items": [
        {"flag": 27, "item_type_id": _id(data, "Heavy Missile Launcher II"), "quantity_destroyed": 1},
        {"flag": 27, "item_type_id": _id(data, "Scourge Heavy Missile"), "quantity_dropped": 40},
        {"flag": 11, "item_type_id": _id(data, "Ballistic Control System II"), "quantity_destroyed": 1},
        {"flag": 87, "item_type_id": _id(data, "Hobgoblin II"), "quantity_destroyed": 2},
        {"flag": 5, "item_type_id": _id(data, "Scourge Heavy Missile"), "quantity_dropped": 500},  # cargo
    ]}, "attackers": [{"faction_id": 500010, "ship_type_id": 1}]}
    f = corpus.fit_from_killmail(km, data)
    assert f.tag == "pve" and f.source == "7"
    assert (27, _id(data, "Heavy Missile Launcher II"), _id(data, "Scourge Heavy Missile")) in f.modules
    assert f.drones == [(_id(data, "Hobgoblin II"), 2)]
    km["attackers"].append({"corporation_id": corpus.CORP_CONCORD, "ship_type_id": 1})
    assert corpus.fit_from_killmail(km, data).tag == "pvp"  # a ganker killed by CONCORD
    km["attackers"] = [{"faction_id": 500010}, {"character_id": 123, "ship_type_id": 1}]
    assert corpus.fit_from_killmail(km, data).tag == "pvp"
    km["victim"]["ship_type_id"] = 670  # a capsule is not a fit
    assert corpus.fit_from_killmail(km, data) is None


def test_wiki_shipfitting_template(data):
    block = """
| ship=Caracal
| fitName=Test
| high1name=Heavy Missile Launcher II
| high2name=Heavy Missile Launcher II
| mid1name=Large Shield Extender II
| low1name=Ballistic Control System II
| rig1name=Medium Core Defense Field Extender I
| drone1name=Hobgoblin II x2
| charge1name=Scourge Heavy Missile x1000"""
    f = corpus.parse_shipfitting(block, data, "page")
    assert f.tag == "curated" and len(f.modules) == 5
    launcher = [m for m in f.modules if m[0] == 27][0]
    assert launcher[2] == _id(data, "Scourge Heavy Missile")
    assert f.drones == [(_id(data, "Hobgoblin II"), 2)]
    assert "Heavy Missile Launcher II, Scourge Heavy Missile" in f.eft(data)


RIFTER_TOP = ["200mm AutoCannon II"] * 3 + ["1MN Afterburner II", "Warp Scrambler II", "Stasis Webifier II"]


def _real(data, ship, lows, tag="pve", source=""):
    mods = [(27 + i, _id(data, n), None) for i, n in enumerate(RIFTER_TOP[:3])]
    mods += [(19 + i, _id(data, n), None) for i, n in enumerate(RIFTER_TOP[3:])]
    mods += [(11 + i, _id(data, n), None) for i, n in enumerate(lows)]
    return corpus.RealFit(_id(data, ship), mods, tag=tag, source=source)


@pytest.fixture(scope="module")
def toy_prior(data):
    fits = [_real(data, "Rifter", ["Gyrostabilizer II", "Gyrostabilizer I", "Damage Control II", "Nanofiber Internal Structure II"])
            for _ in range(30)]
    fits += [_real(data, "Rifter", ["Overdrive Injector System II"] * 4) for _ in range(2)]
    model = prior.train(fits, data, log=lambda m: None)
    return prior.FitPrior(model, data)


def test_families_group_meta_variants(data):
    assert prior.family(data.type_by_name("Gyrostabilizer II")) == prior.family(data.type_by_name("Gyrostabilizer I"))
    assert prior.family(data.type_by_name("Gyrostabilizer II")) != prior.family(data.type_by_name("Damage Control II"))


def test_common_modules_score_higher(data, toy_prior):
    rifter = data.type_by_name("Rifter")
    totals = {"low": 4}

    def score(names):
        return toy_prior.slot_scores(rifter.id, {"low": [data.type_by_name(n) for n in names]}, totals)["low"]

    common = score(["Gyrostabilizer II", "Gyrostabilizer II", "Damage Control II", "Nanofiber Internal Structure II"])
    rare = score(["Overdrive Injector System II"] * 4)
    assert common > rare
    # A slot never costs more than the floor, and an empty slot never beats the best module.
    assert score(["Gyrostabilizer II"] * 4) >= 0 - 1e-9
    assert score([]) <= 0 + 1e-9 and score(["Co-Processor II"] * 4) >= 4 * prior.FLOOR - 1e-9


def test_mostly_empty_fits_are_not_learned(data):
    fits = [corpus.RealFit(_id(data, "Rifter"), [(11, _id(data, "Damage Control II"), None)]) for _ in range(50)]
    model = prior.train(fits, data, log=lambda m: None)
    assert prior.FitPrior(model, data).fits_seen(_id(data, "Rifter")) == 0


def test_top_fits_round_trip(data, toy_prior):
    tops = toy_prior.top_fits(_id(data, "Rifter"), "pve")
    assert tops and tops[0].source == "30 seen"
    fit = Fit.from_eft(tops[0].eft(data), data)
    assert "Damage Control II" in [m.type.name for m in fit.modules] and len(fit.modules) == 10


def test_optimizer_reports_typicality_and_respects_practicality(data, all5):
    spec = from_dict({"ship": "Rifter", "objective": {"maximize": "dps"}, "search": {"effort": "quick"}})
    opt = Optimizer(data, all5, spec)
    if opt.prior is None:
        pytest.skip("no trained fit model")
    best = opt.run()[0].best
    assert 0 < best.stats.extras["typicality"] <= 1
    off = from_dict({"ship": "Rifter", "objective": {"maximize": "dps"},
                     "search": {"effort": "quick", "practicality": 0}})
    o2 = Optimizer(data, all5, off)
    assert o2.prior is None and math.isclose(o2.run()[0].best.practicality, 0.0)


def test_polarized_weapons_are_opt_in(data, all5):
    spec = from_dict({"ship": "Caracal", "objective": {"maximize": "dps"}, "search": {"effort": "quick"}})
    best = Optimizer(data, all5, spec).run()[0].best
    assert not any(m.type.name.startswith("Polarized") for m in best.fit.modules)


def test_reload_counts_in_damage_metrics(data, all5):
    from evesim.optimize.spec import METRICS
    from evesim.stats import compute
    fit = Fit.from_eft("[Caracal, x]\n\n\n" + "Rapid Light Missile Launcher II, Scourge Light Missile\n" * 5, data)
    st = compute(fit, all5)
    with_reload = from_dict({"ship": "Caracal"})
    without = from_dict({"ship": "Caracal", "objective": {"maximize": "dps", "reload": False}})
    assert METRICS["dps"].fn(st, with_reload) < 0.8 * METRICS["dps"].fn(st, without)
    assert METRICS["dps"].fn(st, without) == pytest.approx(st.dps)
