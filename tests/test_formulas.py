"""Pure-formula tests that need no static data."""

import math

import pytest

from evesim import capacitor
from evesim.application import Target, missile_factor, turret_factor
from evesim.dogma.engine import _penalized_product
from evesim.optimize import spec as specmod
from evesim.stats import Weapon, normalize_profile


def test_stacking_penalty_matches_known_values():
    # Three 10% damage mods: 1.1 * 1.0869 * 1.0571
    assert _penalized_product([1.1, 1.1, 1.1]) == pytest.approx(1.1 * (1 + 0.1 * 0.8691) * (1 + 0.1 * 0.5706), rel=1e-4)
    # Bonuses and penalties are penalized separately, strongest first.
    assert _penalized_product([1.2, 0.8]) == pytest.approx(1.2 * 0.8)
    assert _penalized_product([1.05, 1.3]) == pytest.approx(1.3 * (1 + 0.05 * math.exp(-1 / 2.67 ** 2)))


def test_cap_stable_and_unstable():
    stable = capacitor.simulate(1000, 200_000, [capacitor.CapUser("x", 10, 5)])
    assert stable.stable and 0 < stable.stable_fraction < 1
    unstable = capacitor.simulate(250, 93_750, [capacitor.CapUser("x", 40, 4.5), capacitor.CapUser("y", 30, 10)])
    assert not unstable.stable and unstable.lasts_s > 0
    # Peak recharge is 2.5 * C / tau.
    assert unstable.peak_recharge == pytest.approx(2.5 * 250 / 93.75)


def test_cap_booster_injects():
    users = [capacitor.CapUser("drain", 60, 4), capacitor.CapUser("booster", -400, 12, charges=1, reload_s=10)]
    r = capacitor.simulate(500, 150_000, users)
    assert r.inject_per_s == pytest.approx(400 / 22)


def test_turret_application():
    w = Weapon("gun", "turret", 1, {"em": 0, "thermal": 0, "kinetic": 10, "explosive": 0}, 2.0,
               optimal=10_000, falloff=5_000, tracking=50)
    still = Target(signature=10_000, velocity=0, distance=5_000)
    assert turret_factor(w, still) == pytest.approx(0.03 + 0.99 * (0.49 + 1.01 / 2))
    # At optimal + falloff with no transversal the chance to hit is 50%.
    edge = Target(signature=10_000, velocity=0, distance=15_000)
    cth = 0.5
    assert turret_factor(w, edge) == pytest.approx(0.03 + (cth - 0.01) * (0.49 + (cth + 0.01) / 2))


def test_missile_application():
    w = Weapon("m", "missile", 1, {"em": 0, "thermal": 0, "kinetic": 100, "explosive": 0}, 10,
               optimal=50_000, explosion_radius=140, explosion_velocity=85, drf=0.682)
    assert missile_factor(w, Target(signature=400, velocity=0, distance=10_000)) == 1.0
    small = Target(signature=35, velocity=400, distance=10_000)
    expect = min(1, 35 / 140, ((35 / 140) * (85 / 400)) ** 0.682)
    assert missile_factor(w, small) == pytest.approx(expect)
    assert missile_factor(w, Target(signature=400, velocity=0, distance=60_000)) == 0.0


def test_profile_normalization():
    p = normalize_profile({"em": 2, "thermal": 2})
    assert p == {"em": 0.5, "thermal": 0.5, "kinetic": 0.0, "explosive": 0.0}
    assert sum(normalize_profile(None).values()) == pytest.approx(1)


def test_spec_parsing():
    sp = specmod.from_dict({
        "ship": "Caracal",
        "objective": {"weights": {"applied_dps": 1, "ehp": 0.5}},
        "target": {"preset": "frigate", "distance": 20000, "resists": {"kinetic": 0.4}},
        "incoming": {"preset": "guristas"},
        "constraints": {"min_ehp": 10000, "cap_stable": True, "budget": 5e7},
        "fitting": {"tank": "shield", "propulsion": "ab", "meta": ["tech1", "tech2"]},
        "search": {"effort": "quick"},
    })
    assert sp.ships == ["Caracal"]
    assert sp.weights == {"applied_dps": 1.0, "ehp": 0.5}
    assert sp.target.distance == 20000 and sp.target.resonance["kinetic"] == pytest.approx(0.6)
    assert sp.incoming["kinetic"] == 82
    kinds = {(c.metric, c.kind) for c in sp.constraints}
    assert kinds == {("ehp", "min"), ("cap_seconds", "min"), ("price", "max")}
    assert sp.cap_simulation_needed


@pytest.mark.parametrize("bad", [
    {"objective": {"maximize": "awesomeness"}},
    {"constraints": {"most_dps": 3}},
    {"fitting": {"tank": "hull-and-prayers"}},
    {"fitting": {"meta": ["tech9"]}},
    {"unknown_section": {}},
])
def test_spec_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        specmod.from_dict(bad)
