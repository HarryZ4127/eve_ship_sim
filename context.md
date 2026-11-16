# evesim: project context

Background for picking this project up locally (or in a new Claude session): what was
built, why, how it fits together, how to run and refresh everything, and what is still
open. Written 2026-11-16 at commit after `d9182fd`. The user-facing docs are
[README.md](README.md), [docs/requirements.md](docs/requirements.md) (spec reference) and
[docs/design.md](docs/design.md) (design and validation); this file links the threads
together and records decisions and history that are not in those.

## 1. What this is

`evesim` is an EVE Online ship simulator and fit optimizer in pure Python (3.11+, no
runtime dependencies):

* a dogma engine that executes CCP's own `modifierInfo` data, validated number by number
  against Pyfa;
* character skills from ESI (EVE SSO with PKCE), JSON files or presets;
* an optimizer that searches modules, weapons, ammo, drones and fighters under
  requirements written in TOML (objectives, constraints, fitting choices), and that has
  learned from 1.2 million real fits what pilots actually fly;
* a skill training advisor;
* a browser demo (Pyodide) with a picture-based fitting window.

* Repository: <https://github.com/HarryZ4127/eve_ship_sim> (branch `main`)
* Browser demo (private Claude artifact, version 7, owner can share it):
  <https://claude.ai/artifact/14CWR6182Mr9FwW4hNvPF1>

## 2. Status snapshot (2026-11-16)

| item | state |
|---|---|
| game data | build 3687575 (Pyfa master, 2026-11-11): 7,603 types, 427 published hulls |
| tests | 100 passing (`pytest`), ~3 minutes |
| Pyfa reference | 24 fits match Pyfa to 0.1% (`tests/data/pyfa_reference.json`) |
| fit model | `src/evesim/data/prior.json.gz`, trained on 120 days of killmails (2026-07-18 to 2026-11-14) + 450 EVE University fits |
| web demo | version 7 published; `web/smoke_test.mjs` passes against a fresh build |
| metrics | 58 (`evesim metrics`) |

## 3. History of the work

In order, what was asked and what came of it:

1. **Simulator + optimizer from the character's skills.** Dogma engine, fit model, EFT
   import/export, stats (DPS, application, EHP, tank, capacitor simulation, navigation,
   targeting), ESI skills, requirements spec, optimizer, CLI.
2. **Browser demo** with Pyodide (the artifact cannot fetch external hosts, `.gz` or
   `.zip` files, so data ships as plain JSON and the Python stdlib as base64).
3. **Bug: T2 ammo in non-T2 weapons** — fixed in `fit.charge_fits` (techLevel rule).
4. **Training advisor; all ships; more objectives; Pareto fronts.** Advisor (single
   trainings, plan, unlocks with re-optimization), ship classes and screening, T3C
   subsystem search, T3D modes, roles without damage (mining, logistics, cargo), EWAR
   metrics, Pareto fronts over any number of metrics.
5. **Fighters** for carriers and supercarriers (squadrons, tubes, abilities like Pyfa).
6. **Fitting window with pictures** (ship renders and module icons from Pyfa's `imgs`,
   packed into two atlases; slot ring, market browser, live stats).
7. **Exploration and harvesting:** probe strength, data/relic hacking, salvage, ice and gas
   yield, mining residue; later combined `hacking_*` metrics (data and relic together).
8. **The user's own skills** (Harry Zhu) via ESI; login fixes (only request scopes that
   are read; accept a plain character name). Personal fits for exploration (Tengu) and
   the Crimson Harvest event (Rattlesnake / Praxis / Myrmidon / Typhoon / Armageddon).
9. **New game build** (Navy attack battlecruisers, Penitence, Herald, Synchronizer rigs)
   and **learning from real fits** (corpus + prior model), because optimized fits were
   legal but not something anyone would fly.
10. **Interrupted:** a lowsec faction warfare PvP fit (see section 12).

## 4. Local setup

PowerShell (the user's machine is Windows; bash equivalents in comments):

```powershell
git clone https://github.com/HarryZ4127/eve_ship_sim.git   # or: git pull
cd eve_ship_sim
python -m venv .venv
.venv\Scripts\Activate.ps1                     # bash: source .venv/bin/activate
pip install -e ".[dev]"
evesim data update                             # ~20 s, writes ~/.cache/evesim/staticdata.json.gz
pytest                                         # 100 tests, ~3 min
evesim optimize examples/caracal_pve.toml --skills all5
```

Character skills (one-time app registration at <https://developers.eveonline.com/>:
"Authentication & API Access", callback `http://localhost:8642/callback`, scopes
`esi-skills.read_skills.v1` and `esi-clones.read_implants.v1`):

```powershell
$env:EVESIM_CLIENT_ID = "<client id>"          # bash: export EVESIM_CLIENT_ID=...
evesim login                                   # tokens -> ~/.config/evesim/tokens.json
evesim skills -s "esi:Harry Zhu" -o me.json    # snapshot for offline use / the web demo
evesim optimize examples/tengu_explorer.toml --skills me.json
```

`--skills` accepts `all0`..`all5`, `alpha <race>`, `esi:<name>`, a logged-in character's
plain name, `esi` (the only logged-in character), or a JSON file. Never commit `me.json`
or tokens (`.gitignore` covers `*.json.gz` but not arbitrary JSON).

## 5. Repository map

```
src/evesim/
  data/build.py        static data builder: Pyfa's staticdata/fsd_built (GitHub raw or a local
                       checkout) -> compact gzip JSON (types, attributes, effects, groups,
                       market groups, icon/graphic ids, clone grades)
  data/store.py        StaticData, Type (attrs, effects, required skills, family/meta info)
  data/prior.json.gz   the trained fit model (shipped; ~530 KiB)
  dogma/engine.py      Character (skills+implants, computed once) and FitCalc (per fit)
  dogma/const.py       attribute/effect ids, operations, states
  fit.py               Fit/ModuleSpec/DroneSpec/FighterSpec, EFT, fitting-rule checks
  stats.py             derived stats (weapons, tank, cap, nav, targeting, utility)
  capacitor.py         time-domain capacitor simulation (Pyfa-style stagger)
  application.py       applied damage vs a Target (turret, missile, drone, fighter)
  skills/profile.py    skill profiles (presets, JSON, ESI format)
  skills/esi.py        EVE SSO (PKCE), token store, ESI reads
  market.py            ESI average prices (daily cache), static base price fallback
  optimize/spec.py     Spec (TOML), METRICS registry, metric->attribute groups
  optimize/search.py   Optimizer (candidate pools, weapons, greedy/local/iterated search,
                       real-fit seeds, practicality term, fill, drones/fighters, ammo, cap check)
  optimize/pareto.py   trade-off frontier over N metrics
  advisor.py           training advisor
  corpus.py            real fits: EVE Ref killmail archives, EVE University wiki
  prior.py             the fit model: training, typicality scoring, top real fits
  report.py, cli.py    text output, command line
web/                   index.html (single page), worker.js (Pyodide worker), webapi.py
                       (Python bridge), build.py (assembles dist), smoke_test.mjs
tools/                 pyfa_reference.py (Pyfa eos headless), wx_stub_sitecustomize.py,
                       audit_ships.py (every hull computes; one optimize per group)
tests/                 engine/formula/feature/optimizer/ESI/prior tests, Pyfa comparison
examples/              spec files (PvE, kiting, budget, Pareto, exploration, Crimson
                       Harvest, Tengu explorer, Ishtar ratting)
```

## 6. How it works (decisions worth knowing)

**Data.** CCP endpoints were blocked in the original cloud session, so static data comes
from Pyfa's repository copy of the FSD export (refreshed every patch, includes
`modifierInfo`). Only fitting-relevant categories are kept. T3D tactical modes are kept
even though unpublished.

**Engine.** Executes modifierInfo directly (ItemModifier, LocationModifier,
LocationGroupModifier, LocationRequiredSkillModifier, OwnerRequiredSkillModifier).
Operation order preAssign, preMul, preDiv, modAdd, modSub, postMul, postDiv, postPercent,
postAssign; stacking penalty `exp(-(i/2.67)^2)` per operation, ships/charges/skills/
implants/subsystems exempt. Hand-written: propulsion (mass, speed, MWD sig), subsystem
slots/hardpoints, Reactive Armor Hardener at its start split, skill self-bonus effects.
Synchronizer rigs (faction module sets) need no special code: the set count flows through
modifierInfo, verified against Pyfa.

**Stats conventions.** Like Pyfa: DPS without reloads in stats/reports; drones active in
listed order up to 5 within bandwidth; fighters' default abilities, reload ignored.
Burst modules (reactivation delay > 0: MJD, cloaks, assault damage controls) default to
online, not active — this is why reference fits avoid MJDs and cloaks (Pyfa activates
them). Nosferatu are not counted as cap gain unless `--assume-nos`.

**Capacitor simulation.** Event-driven; non-turret modules with identical cap use and
cycle are staggered as Pyfa does (grouped by numbers, not names); cap boosters inject as
needed. Optimizer searches with cap averages and re-checks finalists with the simulation.

**Optimizer score.** `sum(w * metric / scale)` + practicality term + tiny tie-breaker
(less CPU/PG used against the bare hull's output, lower price) − 1000 × relative
constraint violation. Default scales in `search.py` (`DEFAULT_SCALES`: 100 DPS, 10,000
EHP, 100 EHP/s, 1,000 m/s ... = 1 point). Damage metrics count reloads by default
(`[objective] reload`), Polarized weapons are opt-in (`[fitting] polarized`).

**Search pipeline per hull.** (1) candidate pools per weapon setup: every usable module,
kept if it improves a relevant metric on the armed hull or on a probe with "enablers"
(booster, repairer, cap booster, prop, mining laser, remote rep, analyzer, salvager,
probe launcher by group), Pareto-pruned within a family; the prior's most common families
per slot are added. (2) weapon setups: rank every turret/launcher with probe charges,
try all charges on the leaders, add the cheapest decent one and the hull's most common
weapon; mixed turret+launcher only where real fits mix. (3) per setup: greedy fill,
coordinate descent, iterated local search. (4) real-fit seeds (quick 2, normal 4,
thorough 8): the hull's most common real fits adapted to the character (unusable modules
→ best usable variant of the same family), polished by local search; weaponless seeds also
tried with the best weapon setup. (5) fill empty slots (EHP/DPS + typicality, never
lowering the objective), drones/fighters knapsack, ammo re-pick, cap simulation check.
Many hulls: screen with greedy fits, full search on `screen_top`.

**Fit model (prior).** `corpus.py` keeps victim fits from EVE Ref's daily ESI killmail
archives (`data.everef.net/killmails/YYYY/killmails-YYYY-MM-DD.tar.bz2`, ~10k fits/day,
2-3 MB). Tags: `pve` if no player attacker **and** neither CONCORD (corp 1000125) nor an
empire faction (500001-500004, 500006) took part — gankers killed by CONCORD were a
quarter of NPC-only kills and polluted PvE with PvP fits; otherwise `pvp`. EVE University
wiki `{{ShipFitting}}` templates (MediaWiki API) give `curated` fits (weight 10). Fits
with < 60% of high/mid/low slots filled are skipped. `prior.py` learns, per hull and slot
type, a multinomial over module families (variation parent; unmarketed modules by group)
including "empty", smoothed hull → ship group → global, PvE smoothed towards all.
Score per slot: `0.05 * practicality * max(log(p/p_best_module), -4)`; empty never beats
the best module. Typicality = geometric mean of `p/p_best` over slots. It also stores the
top 6 complete fits per hull and tag and the most loaded charge per (hull, module).

Results on the test cases (all-V, normal effort; DPS counts reloads):

| case | prior off | prior on |
|---|---|---|
| Praxis, Crimson Harvest | 835 DPS, typicality 0.19 | 864 DPS, 0.36 |
| Rattlesnake, same | 869, 0.10 | 896, 0.49 |
| Myrmidon, same | 578, 0.06 | 550, 0.32 |
| Ishtar ratting | 547 DPS / 2,092 EHP/s, 0.03 | 675 / 2,670, 0.33 |
| Tengu explorer | 642 DPS, probe 130, 0.37 | 673 DPS, probe 111, 0.52 |

Tuning knobs if results drift: `PRACTICALITY_POINTS` (0.05), `FLOOR` (−4),
`TYPICAL_PER_SLOT` (8), `SEEDS`, smoothing `K_GROUP/K_HULL/K_TAG` and `MIN_FILLED` in
`prior.py`. Halving the weight from 0.1 to 0.05 fixed a Myrmidon that dropped all guns.

**Training advisor.** SP = `250 * rank * sqrt(32)^(L-1)`; speed = primary + secondary/2
per minute; greedy plan by gain per day; unlocks re-optimize the hull and compare with the
best fit current skills can fly (avoids crediting search noise).

## 7. Validation (Pyfa)

`tests/test_against_pyfa.py` compares every fit in `tests/data/pyfa_reference.json`
with Pyfa's eos engine; it skips when the static data's client build differs from the
reference's. After a game patch, regenerate it:

```bash
git clone https://github.com/pyfa-org/Pyfa pyfa
python -m venv pyfa-venv && pyfa-venv/bin/pip install sqlalchemy logbook numpy python-dateutil \
    requests pyyaml roman packaging beautifulsoup4 markdown2 cryptography python-jose requests-cache
mkdir stubs && cp tools/wx_stub_sitecustomize.py stubs/sitecustomize.py
(cd pyfa && PYTHONPATH=$PWD/../stubs ../pyfa-venv/bin/python db_update.py)
python -c "import json; json.dump(json.load(open('tests/data/pyfa_reference.json'))['fits'], open('fits.json','w'))"
(cd pyfa && PYTHONPATH=$PWD/../stubs ../pyfa-venv/bin/python ../tools/pyfa_reference.py $PWD ../fits.json > ../ref.json)
# then write ref.json into the reference's "expected", set "client_build", and run pytest
```

Reference fits avoid burst modules (MJD, cloaks) because of the default-state difference.

## 8. Runbook: a new game patch

1. `evesim data update` (check `evesim data info` for the client build).
2. `pytest` — the Pyfa test skips on a new build; regenerate the reference (section 7)
   and add fits for any new mechanic (as done for Synchronizer rigs).
3. `evesim corpus fetch --days 120` then `evesim prior build --out src/evesim/data/prior.json.gz`
   (~10 min download, ~90 s training). Spot-check `evesim fits Caracal`, `evesim fits Rattlesnake`.
4. Rebuild and smoke-test the web demo (section 9).
5. Update README/design numbers (reference count, metrics count, corpus size).

## 9. Web demo

```bash
npm install pyodide@0.27.7 playwright-core
pip install pillow                                   # for the picture atlases
python web/build.py dist --pyodide node_modules/pyodide --images pyfa/imgs
python -m http.server -d dist 8000                   # http://localhost:8000
node web/smoke_test.mjs http://localhost:8000/index.html   # CHROMIUM_PATH=... if needed
```

`build.py` writes `staticdata.json`, `prior.json` (plain JSON; the artifact host serves no
`.gz`), `catalog.json`, `images.json` + `icons.png` + `renders.jpg`, `evesim_src.json`
(all Python sources), the Pyodide runtime (stdlib as base64 text, decoded by a fetch shim
in `worker.js`), and an `index.html` with precomputed example readouts. The page is
self-contained and can be hosted anywhere static. The published copy is a Claude artifact
(only Claude's Artifact tool can republish it; the owner shares it from its Share menu).
The artifact CSP blocks external images and fetches, which is why pictures are bundled.

## 10. Environment variables and caches

| variable | default | what |
|---|---|---|
| `EVESIM_DATA` | `~/.cache/evesim/staticdata.json.gz` | static data file |
| `EVESIM_CORPUS` | `~/.cache/evesim/corpus/` | cached real fits (one `km-DATE.jsonl.gz` per day, `uniwiki.jsonl.gz`) |
| `EVESIM_PRIOR` | `~/.cache/evesim/prior.json.gz` if present, else the shipped model | fit model |
| `EVESIM_CLIENT_ID` | — | EVE SSO application client ID |
| `EVESIM_ESI_BASE` | `https://esi.evetech.net/latest` | ESI base URL |

Tokens: `~/.config/evesim/tokens.json` (mode 600). Prices: daily ESI cache in
`~/.cache/evesim`. In the cloud, the "eve_ship_sim" environment allows
`data.everef.net`, `zkillboard.com`, `esi.evetech.net`, `wiki.eveuniversity.org` and
`eveworkbench.com`, with a setup script that runs `pip install -e '.[dev]'` in a venv.

## 11. The user and their character

* Windows, PowerShell; Python via conda base env (`(base) PS G:\EVE_fit_sim\eve_ship_sim`).
* Character **Harry Zhu**. Skills as of the 2026-10-18 export (170 skills, +3 attribute
  implants, attributes 33-37): Drones V / Heavy, Medium, Light Drone Operation V, Drone
  Interfacing IV; Heavy and Light Missiles V, Cruise IV, HAM III, no Torpedoes; Medium
  Hybrid V, Large Hybrid 0, Large Projectile III; all racial cruisers IV-V (Caldari and
  Gallente V), battleships, battlecruisers and destroyers IV, Heavy Assault Cruisers IV,
  Caldari Strategic Cruiser IV with subsystems V; Hacking and Archaeology V, Astrometrics
  IV (+ Rangefinding/Pinpointing/Acquisition IV), Cloaking IV. Not trained: Assault
  Frigates, Covert Ops, tactical destroyers, Marauders. Can fly 205 of 421 hulls
  (Ishtar, Cerberus, Tengu, Gila, Rattlesnake, Praxis, Nestor, Barghest, Akoman ...).
  The export is not in the repository; refresh it with `evesim skills -s "esi:Harry Zhu" -o me.json`.
* Interests so far: highsec PvE, exploration (scanning + hacking in a Tengu), the Crimson
  Harvest event on a budget (Rattlesnake judged too expensive; Praxis / Typhoon /
  Armageddon / Myrmidon suggested), now lowsec faction warfare PvP.
* Training advice given: Astrometrics V then Astrometric Rangefinding V for exploration.

## 12. Open threads

* **Faction warfare PvP fit (interrupted).** Complex rules found (EVE University wiki,
  2026): sizes Scout (frigates and smaller, 10 min), Small (destroyers, 10 min), Medium
  (cruisers, 15 min), Moderate (battlecruisers), Large (battleships), Open (unrestricted);
  BSC = T1 combat ships only, NVY = T1 + navy, ADV = also T2 and pirate (T3 only in larger
  sizes), ELT = T1/T2/T3 with T3 bonuses. Cradle of War (2026-06-09) added higher-VP
  complexes; a Small AF-1 (assault frigates only) replaced Small Interceptor-1 on
  2026-09-22. Next step: refresh the user's skills, pick a complex size (Small/Medium
  NVY suit the user's skills: e.g. destroyers, Caracal/Vexor Navy class cruisers), and
  run specs with `[search] activity = "pvp"`, `min_tackle_range`, `min_web_strength`,
  `propulsion`, target preset frigate/destroyer at short range, `incoming` uniform.
* **Skills refresh** (above).
* **Ideas not done:** market prices in specs from ESI (host now reachable in the cloud
  environment); EVE Workbench fits (its data comes from a separate API host); weighting
  killmail fits by success (e.g. zKillboard solo kills) rather than counting losses;
  command bursts; projected EWAR on the target; mutated modules; drone travel/application;
  the optimizer still sometimes adds a small booster on a battleship to meet a tank
  constraint exactly (allowed by the prior's floor); a "prefer cap-independent tank"
  option for neut-heavy content.

## 13. Conventions

* Run tests against an explicit data file when several exist:
  `EVESIM_DATA=path/to/staticdata.json.gz pytest` (PowerShell: `$env:EVESIM_DATA=...`).
* Static data and corpora are caches, never committed; the trained prior is the only
  committed data file besides `tests/data/pyfa_reference.json`.
* Every number in docs comes from a run; update docs when a refresh changes them.
* Commits so far end with `Co-Authored-By` / `Claude-Session` trailers from the cloud
  sessions; keep or drop as you like locally.
