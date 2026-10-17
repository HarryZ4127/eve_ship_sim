"""Cross-check against Pyfa (see tools/pyfa_reference.py). The reference was generated
from one game build; when the static data is from another build, balance changes make
the numbers differ, so the test is skipped until the reference is regenerated."""

import json
from pathlib import Path

import pytest

from evesim.fit import Fit
from evesim.stats import compute

REF = json.loads((Path(__file__).parent / "data" / "pyfa_reference.json").read_text())
TOL = 1e-3


def _close(a, b, tol=TOL):
    return abs(a - b) <= tol * max(1.0, abs(b))


@pytest.fixture(scope="module")
def same_build(data):
    if data.meta.get("client_build") != REF["client_build"]:
        pytest.skip(f"reference is for build {REF['client_build']}, data is {data.meta.get('client_build')}")


@pytest.mark.parametrize("name", sorted(REF["fits"]))
def test_fit_matches_pyfa(name, data, all5, same_build):
    exp = REF["expected"][name]
    fit = Fit.from_eft(REF["fits"][name], data)
    st = compute(fit, all5, assume_nos=True)
    bad = []

    def chk(label, mine, theirs, tol=TOL):
        if not _close(mine, theirs, tol):
            bad.append(f"{label}: evesim {mine:.4f} vs pyfa {theirs:.4f}")

    chk("dps", st.dps, exp["dps"])
    chk("drone_dps", st.drone_dps + st.fighter_dps, exp["drone_dps"])  # Pyfa counts fighters as drones
    chk("volley", st.volley, exp["volley"])
    for layer in ("shield", "armor", "hull"):
        chk(f"hp.{layer}", st.hp[layer], exp["hp"][layer])
        chk(f"ehp.{layer}", st.ehp[layer], exp["ehp"][layer])
    for k, v in exp["resonance"].items():
        layer, dt = k.split("_")
        chk(f"resonance.{k}", st.resonance[layer][dt], v)
    chk("cpu", st.resources["cpu"][0], exp["cpu_used"])
    chk("cpu_out", st.resources["cpu"][1], exp["cpu_out"])
    chk("pg", st.resources["power"][0], exp["pg_used"])
    chk("pg_out", st.resources["power"][1], exp["pg_out"])
    chk("calibration", st.resources["calibration"][0], exp["calibration_used"])
    chk("speed", st.nav["max_velocity"], exp["max_velocity"])
    chk("align", st.nav["align_time"], exp["align_time"])
    chk("sig", st.nav["signature_radius"], exp["sig_radius"])
    chk("mass", st.nav["mass"], exp["mass"])
    chk("lock_range", st.targeting["lock_range"], exp["lock_range"])
    chk("scan_res", st.targeting["scan_resolution"], exp["scan_res"])
    chk("cap_capacity", st.cap_capacity, exp["cap_capacity"])
    chk("cap_recharge", st.cap_recharge_s * 1000, exp["cap_recharge_ms"])
    chk("cap_gain", st.cap.peak_recharge + st.cap.inject_per_s, exp["cap"]["recharge_peak"])
    chk("cap_use", st.cap.use_per_s, exp["cap"]["used_per_s"])
    assert st.cap.stable == exp["cap"]["stable"], "cap stability"
    if not exp["cap"]["stable"]:
        chk("cap_lasts", st.cap.lasts_s, exp["cap"]["state"], tol=0.05)
    chk("peak_armor", st.tank_peak["armor"], exp["tank_peak"]["armorRepair"])
    chk("peak_shield", st.tank_peak["shield"], exp["tank_peak"]["shieldRepair"])
    chk("passive_shield", st.tank_peak["passive_shield"], exp["tank_peak"]["passiveShield"])
    chk("sustained_armor", st.tank_sustained["armor"], exp["tank_sustained"]["armorRepair"])
    chk("sustained_shield", st.tank_sustained["shield"], exp["tank_sustained"]["shieldRepair"])
    if "slots" in exp:
        for k in ("high", "med", "low", "rig"):
            assert st.slots[k][1] == exp["slots"][k], f"{k} slots"
        for k in ("turret", "launcher"):
            assert st.hardpoints[k][1] == exp["slots"][k], f"{k} hardpoints"
    if "cargo" in exp:  # utility stats were added to the reference later
        chk("cargo", st.utility["cargo"], exp["cargo"])
        u = st.utility
        chk("mining_yield", u["mining_yield"] + u["ice_yield"] + u["gas_yield"], exp["mining_yield"])
        mods = exp.get("modules", [])
        if any("mining_drain" in m for m in mods):
            waste = sum(m["mining_drain"] - m["mining_yps"] / (1 + m.get("miningCritChance", 0) * m.get("miningCritBonusYield", 0))
                        for m in mods if "mining_drain" in m)
            chk("mining_waste", u["mining_waste"], waste)
        strengths = [m["charge_baseSensorStrength"] for m in mods if "charge_baseSensorStrength" in m]
        if strengths:
            chk("probe_strength", u["probes"][0]["strength"], max(strengths))
        for kind, skill in (("data", "Data"), ("relic", "Relic")):
            ms = [m for m in mods if m["name"].startswith(skill + " Analyzer")]
            if ms:
                chk(f"{kind}_coherence", u["hacking"][kind]["coherence"], max(m["virusCoherence"] for m in ms))
                chk(f"{kind}_strength", u["hacking"][kind]["strength"], max(m["virusStrength"] for m in ms))
        salvagers = [m["accessDifficultyBonus"] for m in mods if m["name"].startswith("Salvager")]
        if salvagers:
            chk("salvage_chance", u["salvage_chance"], max(salvagers))
        for layer in ("shield", "armor", "hull"):
            chk(f"remote_{layer}", st.utility["remote_repair"][layer], exp["remote_reps"][layer])
    assert not bad, "\n".join(bad)
