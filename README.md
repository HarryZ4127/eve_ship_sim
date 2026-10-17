# evesim: EVE Online ship simulator and fit optimizer

`evesim` computes a ship's stats for **your character's actual skills**, and it searches
for the **best fit those skills allow** under requirements you write down (minimum
tank, cap stability, budget, tank type, required modules ...).

* **Simulator.** A generic dogma engine driven by CCP's own `modifierInfo` data, with
  stacking penalties, skill and hull bonuses, subsystems, propulsion, charges, drones and
  fighters.
  It reports fitting, DPS/volley, applied DPS against a target, EHP against a damage
  profile, active/passive tank, a time-domain capacitor simulation, navigation and
  targeting, plus cargo, ore/ice/gas yield, scan probe strength, data/relic analyzer
  coherence and strength, salvage chance, remote repair output and EWAR/tackle strength.
  Across 21 reference fits, every number matches [Pyfa](https://github.com/pyfa-org/Pyfa)
  (see [Validation](docs/design.md#validation)). All 421 hulls compute; T3 cruiser
  subsystems, T3 destroyer modes and carrier/supercarrier fighters are supported.
* **Skills.** Pull them from ESI with an EVE SSO login, load a JSON file, or use
  presets (`all5`, `all0`, or alpha clone caps like `alpha caldari`).
* **Optimizer.** Searches the modules, weapons, ammo, drones and fighters your skills allow,
  scoring every candidate with the exact simulator. You set the objective (one metric,
  a weighted mix of any number, or a Pareto trade-off frontier) and the constraints in a
  short TOML file. It can also pick the best hull from a whole ship class.
* **Training advisor.** Ranks skills by how much one more level improves your objective
  per day of training at your attributes, builds a plan for a day budget, and finds
  skills that unlock a better fit.

## Quick start

```bash
pip install -e .            # Python 3.11+, no runtime dependencies
evesim data update          # download + build static data (~10 s, cached in ~/.cache/evesim)

evesim stats my_fit.eft --skills all5 --profile guristas --target cruiser --distance 20000
evesim optimize examples/caracal_pve.toml --skills all5
evesim train my_fit.eft --skills me.json --objective applied_dps=1,ehp=0.5 --days 30
```

### Using your character's skills

1. Register an application at <https://developers.eveonline.com/>: choose
   *Authentication & API Access*, set the callback to `http://localhost:8642/callback`, and
   add the scopes `esi-skills.read_skills.v1`, `esi-clones.read_implants.v1` and
   `esi-fittings.write_fittings.v1`.
2. `export EVESIM_CLIENT_ID=<your client id>`. The login uses PKCE, so no secret is stored.
3. `evesim login`: a browser opens for EVE SSO, and tokens are saved to `~/.config/evesim/tokens.json`.
4. Use `--skills esi:<character name>` with `stats`, `optimize`, `train` or `skills`.

ESI returns `active_skill_level`, so alpha clone caps are applied automatically, and
your attributes, which set training speed for the advisor.
`evesim skills -s esi:Name -o me.json` saves a snapshot you can reuse offline, and that
file is what you upload in the browser demo ("Upload skills JSON"). The demo cannot reach
ESI itself, because the page is not allowed to contact other sites.

Without registering an application, you can also save the JSON response of
`GET /characters/{character_id}/skills` from ESI's API explorer
(<https://developers.eveonline.com/api-explorer>), signed in with your character and the
`esi-skills.read_skills.v1` scope. That file loads as is (`--skills skills.json`). It has no
attributes, so pass `--attributes` to the training advisor or it assumes 20 each.

A hand-written file works too: `{"skills": {"Gunnery": 5, "Astrometrics": 4}}` (skills
left out count as untrained).

## Writing requirements

```toml
ship = "Caracal"                      # or ships = [...], ship_classes = ["cruiser"], ships = "all"

[objective]
maximize = "applied_dps"              # or: weights = { applied_dps = 1.0, ehp = 0.5, speed = 0.2 }
                                      # or: pareto = ["applied_dps", "ehp", "speed"]  (trade-off frontier)

[target]                              # what you shoot (for applied_dps)
preset = "cruiser"                    # frigate | destroyer | cruiser | battlecruiser | battleship | structure
distance = 30000

[incoming]                            # what shoots you (for ehp / tank)
preset = "guristas"                   # or em/thermal/kinetic/explosive weights
dps = 400                             # only needed for survival_time

[constraints]
min_ehp = 12000
cap_stable = true
max_price = 80_000_000                # ISK, from ESI average prices

[fitting]
tank = "shield"                       # shield | armor | hull | any
propulsion = "ab"                     # mwd | ab | none | any
meta = ["tech1", "tech2", "storyline"]
require = ["Warp Scrambler II"]
exclude = ["Polarized Heavy Assault Missile Launcher"]
```

`evesim metrics` lists the 54 metrics you can maximize or constrain:

| area | metrics |
|---|---|
| damage | `dps`, `applied_dps`, `weapon_dps`, `drone_dps`, `fighter_dps`, `volley`, `weapon_range` |
| tank | `ehp`, `shield_ehp`, `armor_ehp`, `hull_ehp`, `sustained_tank`, `peak_tank`, `survival_time` |
| capacitor | `cap_seconds`, `cap_stable_fraction`, `cap_delta`, `cap_capacity`, `cap_recharge` |
| movement | `speed`, `align_time`, `signature`, `mass`, `warp_speed` |
| targeting | `lock_range`, `lock_time`, `scan_resolution`, `max_targets`, `sensor_strength`, `drone_control_range` |
| EWAR and tackle | `neut_rate`, `tackle_range`, `web_range`, `web_strength`, `paint_strength`, `ecm_strength`, `damp_strength`, `disruption_strength` |
| roles | `remote_repair`, `cargo` |
| mining | `mining_yield` (ore), `ice_yield`, `gas_yield` |
| exploration | `probe_strength`, `data_coherence`, `data_strength`, `relic_coherence`, `relic_strength`, `salvage_chance` |
| cost and fitting | `price`, `dps_per_misk`, `ehp_per_misk`, `cpu_left`, `pg_left` |

[docs/requirements.md](docs/requirements.md) documents every field and how to choose good requirements.

Example output (all-V skills, `examples/caracal_pve.toml`):

```
## Caracal: FEASIBLE  score 6.217  (6937 fits evaluated in 17.3 s)
Objective: 1 x applied_dps
   applied_dps      621.6 HP/s
Constraints:
   [ok] ehp                13,375.0 >= 12000
   [ok] cap_seconds             inf stable

[Caracal, Caracal PvE]
'Full Duplex' Ballistic Control System
...
Polarized Heavy Assault Missile Launcher, Dread Guristas Nova Heavy Assault Missile
...
```

The full report follows with the complete stats of the chosen fit and the runner-up weapon setups.
Note the Polarized launchers: they zero your resists, which is a legal choice when EHP
is only a lower bound. Add them to `exclude` if you don't want that trade.

## Commands

| command | what it does |
|---|---|
| `evesim data update [--source PYFA_DIR]` | build static data from Pyfa's FSD dump (GitHub, or a local checkout) |
| `evesim find TEXT` | search item names |
| `evesim login` / `evesim characters` | EVE SSO login, list logged-in characters |
| `evesim skills -s SPEC [-o file]` | show or save a skill profile |
| `evesim stats FIT.eft` | full stats for an EFT fit (`-` reads stdin); `--json` for machine output |
| `evesim optimize SPEC.toml` | find the best fit (or a Pareto frontier); `--effort quick/normal/thorough`, `--eft-out`, `--json` |
| `evesim train FIT.eft` | skill training advice for a fit; `--days`, `--refit` (unlocks), `--spec` or `--objective` |
| `evesim metrics` | list objective/constraint metrics |

Fits use the EFT text format (fighter squadrons as `Templar II x6`) that the game client (*Copy to clipboard* / *Import from
clipboard*) and Pyfa read and write.

## Status and limits

Not modelled yet: command bursts, the effect of your webs and paints
on your own applied damage, mutated modules, drugs, and Reactive Armor Hardener
adaptation. EWAR is reported as each module's strength and range, not as its effect on a
particular target. The optimizer is a heuristic over an exact simulator, so it is not
guaranteed globally optimal. [docs/design.md](docs/design.md) covers how it works, what
was validated, and open decisions; `tools/audit_ships.py` checks coverage of every hull.

Static data comes from Pyfa's copy of CCP's FSD export because it is reachable,
refreshed every patch, and already includes `modifierInfo`. EVE Online and all related
data are property of CCP hf.

## Browser demo

`web/` holds a self-contained page that runs evesim under [Pyodide](https://pyodide.org)
in a background worker. It has three tabs:

* **Fitting:** a fitting window in the style of the game's. Ship renders and module icons
  sit on a slot ring with CPU/powergrid gauges, a market-tree browser lists every ship,
  module, charge, drone and fighter (filtered to what fits the selected slot and hull), and
  stats update live as you fit, change module states, load charges or fill the drone bay
  and fighter tubes. EFT import and export are built in.
* **Optimize:** weighted objectives, a trade-off frontier, or the best hull of a class.
  Results open in the fitting window.
* **Train skills:** the training advisor.

Build it with

```bash
npm install pyodide@0.27.7
pip install pillow                   # only for the pictures
python web/build.py dist/ --pyodide node_modules/pyodide --images PYFA_CHECKOUT/imgs
python -m http.server -d dist 8000   # then open http://localhost:8000
```

Pictures come from a [Pyfa](https://github.com/pyfa-org/Pyfa) checkout's `imgs` folder
(icons by icon ID, ship renders by graphic ID) and are packed into two atlas images. They
are CCP's artwork and are not stored in this repository. Without `--images` the page uses
lettered tiles.

Skill presets and uploaded skill JSON work there. ESI login needs the command line.
