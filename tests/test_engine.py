"""Engine, fitting rules, EFT and skill-profile tests on real static data."""

import pytest

from evesim.dogma import const as C
from evesim.fit import Fit, check
from evesim.skills.profile import all_level, clone_grade, from_dict
from evesim.stats import compute

RIFTER = """[Rifter, test]
Gyrostabilizer II
Gyrostabilizer II
Damage Control II

5MN Microwarpdrive II
Warp Scrambler II
Stasis Webifier II

200mm AutoCannon II, Republic Fleet EMP S
200mm AutoCannon II, Republic Fleet EMP S
200mm AutoCannon II, Republic Fleet EMP S

Small Projectile Burst Aerator I
Small Projectile Collision Accelerator I
"""


def test_eft_roundtrip(data):
    fit = Fit.from_eft(RIFTER, data)
    again = Fit.from_eft(fit.to_eft(), data)
    assert [m.type.id for m in again.modules] == [m.type.id for m in fit.modules]
    assert [m.charge.id if m.charge else None for m in again.modules] == \
           [m.charge.id if m.charge else None for m in fit.modules]


def test_skills_change_results(data):
    fit = Fit.from_eft(RIFTER, data)
    weak = all_level(data, 1).character(data)
    strong = all_level(data, 5).character(data)
    s_weak, s_strong = compute(fit, weak), compute(fit, strong)
    assert s_strong.dps > s_weak.dps
    assert s_strong.ehp_total > s_weak.ehp_total
    # All-I cannot use T2 guns: the fit is reported illegal.
    assert any("needs" in p for p in s_weak.problems)


def test_stacking_penalty_applies_to_damage_mods(data, all5):
    base = Fit.from_eft("[Rifter, x]\n200mm AutoCannon II, Republic Fleet EMP S\n", data)
    one = Fit.from_eft("[Rifter, x]\nGyrostabilizer II\n200mm AutoCannon II, Republic Fleet EMP S\n", data)
    two = Fit.from_eft("[Rifter, x]\nGyrostabilizer II\nGyrostabilizer II\n200mm AutoCannon II, Republic Fleet EMP S\n", data)
    d0, d1, d2 = (compute(f, all5).dps for f in (base, one, two))
    first, second = d1 / d0, d2 / d1
    assert first > second > 1.0  # the second gyro is penalized


def test_mwd_speed_and_signature(data, all5):
    off = Fit.from_eft("[Rifter, x]\n5MN Microwarpdrive II /OFFLINE\n", data)
    on = Fit.from_eft("[Rifter, x]\n5MN Microwarpdrive II\n", data)
    s_off, s_on = compute(off, all5), compute(on, all5)
    assert s_on.nav["max_velocity"] > 4 * s_off.nav["max_velocity"]
    assert s_on.nav["signature_radius"] > 4 * s_off.nav["signature_radius"]
    assert s_on.nav["mass"] == pytest.approx(s_off.nav["mass"] + 500_000)


def test_fitting_rules(data, all5):
    too_many = Fit.from_eft("[Rifter, x]\n" + "Damage Control II\n" * 2, data)
    problems = check(too_many, all5)
    assert any("at most 1" in p for p in problems)
    wrong_rig = Fit.from_eft("[Rifter, x]\nMedium Core Defense Field Extender I\n", data)
    assert any("cannot be fitted" in p for p in check(wrong_rig, all5))


def test_subsystems_add_slots(data, all5):
    loki = Fit.from_eft("""[Loki, x]
Loki Core - Augmented Nuclear Reactor
Loki Defensive - Covert Reconfiguration
Loki Offensive - Projectile Scoping Array
Loki Propulsion - Intercalated Nanofibers
""", data)
    st = compute(loki, all5)
    assert st.slots["high"][1] == 8 and st.slots["med"][1] == 3 and st.slots["low"][1] == 6
    assert st.hardpoints["turret"][1] == 5


def test_alpha_clone_caps(data):
    alpha = clone_grade(data, "alpha caldari")
    assert 0 < len(alpha.levels) < len(data.skills)
    char = alpha.character(data)
    assert char.level(3300) == 5  # Gunnery trains to V on alpha
    assert any(lvl < 5 for lvl in alpha.levels.values())  # but many skills are capped


def test_esi_skill_json(data):
    prof = from_dict(data, {"skills": [{"skill_id": 3300, "active_skill_level": 3, "trained_skill_level": 5}]})
    assert prof.levels == {3300: 3}  # active level wins (alpha clones)
    prof = from_dict(data, {"name": "x", "skills": {"Gunnery": 4, "3301": 2}, "implants": []})
    assert prof.levels == {3300: 4, 3301: 2}


def test_missile_damage_uses_character_multiplier(data, all5):
    fit = Fit.from_eft("""[Caracal, x]
Ballistic Control System II
Heavy Missile Launcher II, Scourge Heavy Missile
""", data)
    calc = fit.calc(all5)
    assert calc.char_attr("missileDamageMultiplier") == pytest.approx(1.1)
    st = compute(fit, all5, calc=calc)
    w = next(w for w in st.weapons if w.kind == "missile")
    assert w.volley["kinetic"] > data.type_by_name("Scourge Heavy Missile").attr(C.DAMAGE_ATTRS[2]) * 1.3


@pytest.mark.parametrize("weapon, charge, ok", [
    ("Heavy Electron Blaster II", "Void M", True),
    ("Heavy Electron Blaster II", "Antimatter Charge M", True),
    ("'Slither' Heavy Electron Blaster I", "Void M", False),   # storyline gun, T2 ammo
    ("Heavy Electron Blaster I", "Void M", False),
    ("Domination Rocket Launcher", "Mjolnir Javelin Rocket", False),  # faction launcher
    ("Rocket Launcher II", "Mjolnir Javelin Rocket", True),
    ("Polarized Rocket Launcher", "Mjolnir Javelin Rocket", True),     # polarized is T2
])
def test_tech2_charges_need_tech2_weapons(data, all5, weapon, charge, ok):
    from evesim.fit import charge_fits
    assert charge_fits(data.type_by_name(weapon), data.type_by_name(charge)) is ok
    fit = Fit.from_eft(f"[Vexor, x]\n{weapon}, {charge}\n", data) if "Rocket" not in weapon else \
        Fit.from_eft(f"[Kestrel, x]\n{weapon}, {charge}\n", data)
    loaded = [p for p in check(fit, all5) if "cannot be loaded" in p]
    assert bool(loaded) is not ok
