# Design notes

## Layout

```
src/evesim/
  data/build.py      static data builder (Pyfa FSD dump -> compact gzip JSON)
  data/store.py      StaticData: types, dogma attributes/effects, lookups
  dogma/engine.py    generic dogma engine (Character, FitCalc)
  fit.py             Fit model, EFT import/export, fitting-rule checks
  stats.py           derived stats (DPS, EHP, tank, cap, navigation, targeting)
  capacitor.py       time-domain capacitor simulation
  application.py     applied DPS against a target (turret/missile formulas)
  skills/            skill profiles: presets, JSON, ESI (SSO + PKCE)
  market.py          ESI average prices with a daily cache
  optimize/spec.py   requirements spec, metric registry
  optimize/search.py optimizer
  optimize/pareto.py trade-off frontier over several metrics
  advisor.py         skill training advisor
  report.py, cli.py  output and command line
```

## Data

CCP's static data includes `modifierInfo` for 94% of dogma effects. Each entry says
which attribute, on which items (the ship, the character, items requiring a skill,
items in a group ...), is changed by which attribute of the source, using which
operation. The engine executes these entries directly, so it needs no hand-written
table of 3,000+ effects. That keeps it small and lets it follow balance patches
automatically.

The data is taken from Pyfa's repository copy of the FSD export
(`staticdata/fsd_built`), which tracks every patch. `evesim data update` keeps only
fitting-relevant categories and English names, about 0.45 MB gzipped. Each type also
keeps its icon and graphic IDs, and the market group tree is kept, so a user interface
can show pictures and browse items the way the game does.

## Engine rules

* Operation order: preAssign, preMul, preDiv, modAdd, modSub, postMul, postDiv,
  postPercent, postAssign; then min/max capping attributes (for example resonance ≤ 1).
* Stacking penalty `exp(-(i/2.67)^2)` on non-stackable attributes for multiplicative
  operations, applied per operation. Bonuses and maluses are penalized separately,
  strongest first. Ships, charges, skills, implants and subsystems are exempt as sources.
* Effect activity follows module state: passive always, online effects when online,
  active effects when active, overload effects when overheated.
* Targets: `LocationModifier`, `LocationGroupModifier` and `LocationRequiredSkillModifier`
  on the ship hit fitted modules and loaded charges. `OwnerRequiredSkillModifier` hits
  modules, charges and drones. `skillTypeID = -1` means the source skill itself.
* Skill and implant values are computed once per character and shared by every fit,
  which keeps a full evaluation at about 1.5 ms (3.5 ms with the cap simulation).

Effects CCP ships without `modifierInfo` that matter for fitting are implemented by
hand. There are three groups:

1. Propulsion: mass addition, a speed boost computed from the post-addition mass, and
   MWD signature bloom.
2. Subsystem slots and hardpoints.
3. Reactive Armor Hardener at its starting split, plus the "items requiring this skill
   get +x% per level" skill effects (missile and drone damage, specialization rate of
   fire).

Missile damage is the charge's damage times the character's `missileDamageMultiplier`
(Ballistic Controls modify the character), as in the game.

## Validation

`tests/test_against_pyfa.py` compares evesim with Pyfa's `eos` engine, run headless
via `tools/pyfa_reference.py`, on sixteen fits: projectile, missile, drone, laser,
hybrid, an ancillary repairer with cap booster, a T3C with subsystems, a cruise missile
battleship, two T3 destroyers in different tactical modes, a mining barge, shield and
armor logistics, four carriers/supercarriers with fighters, and five exploration and
harvesting fits (probes, data/relic analyzers, salvagers, ice and gas). Every compared value matches to 0.1% (capacitor time-to-empty is
checked to 5% and currently matches exactly): DPS, volley, drone DPS,
HP/EHP per layer, all 12 resists, CPU/PG/calibration, speed, align time, signature,
mass, lock range, scan resolution, capacitor capacity/recharge/usage, stability and
time-to-empty, peak/sustained repair rates, cargo, mining yield and residue, remote
repair, slot and hardpoint counts, probe strength, analyzer coherence and strength, and
salvage chance. Like Pyfa, the capacitor simulation staggers non-turret modules with the
same cap use and cycle (even different items, such as a data and a relic analyzer). The reference carries the game build
it was made from, and the test skips on a different build until regenerated.

Known small differences:

* The "stable at X%" figure on fits with capacitor boosters can differ from Pyfa by a
  few points, because booster timing is modelled slightly differently. Stability itself
  agrees.
* Nosferatu are not counted as capacitor gain unless `--assume-nos` is given. Pyfa
  counts them by default; an optimizer should not rely on a target having capacitor.
* The optimizer's `sustained_tank` counts ancillary repairer reloads (Pyfa's default
  sustained tank does not). Otherwise the search treats an AAR as infinitely
  sustainable.

## Optimizer

The problem: choose modules for each slot, weapons, ammo and drones, subject to slot
counts, hardpoints, CPU/PG/calibration, group limits, skills and user constraints,
maximizing a non-linear objective (stacking penalties, multiplicative bonuses,
capacitor dynamics). This is a combinatorial problem with an expensive black-box
objective. Linear or constraint solvers would need the dogma rules linearized, which
is exactly where they would stop being correct. evesim instead keeps the evaluation
exact and makes the search smart.

1. **Candidates.** All modules usable with the character's skills, allowed by the spec,
   fittable on the hull, and plausibly powerable (no capital modules on cruisers;
   propulsion sized to the hull's mass class). Each is evaluated alone on two probes:
   the armed hull, and the armed hull with enablers (a booster, repairer, cap booster,
   prop mod). Modules that improve no relevant metric on either probe are dropped,
   which keeps boost amplifiers and similar synergy modules. Within a module family
   (T1/T2/named variants of the same item), variants dominated on benefit vs.
   CPU/PG/calibration/price are dropped. Different modules are never compared with
   each other, because their value depends on the rest of the fit.
2. **Weapons.** Every turret/launcher type is ranked on the bare hull with two probe
   charges (most damage, most range). The best few per family then try every allowed
   charge, and the top configurations go forward. One extra config is the cheapest to
   fit among decent options, since grid freed from weapons can go to tank. Mixed
   turret/launcher hulls get the other weapon kind in leftover high slots.
3. **Search** per weapon configuration: greedy fill, then coordinate descent (for each
   fitted module or empty slot, try every alternative and keep the best, until a pass
   changes nothing), then iterated local search with random perturbations. Moves that
   would obviously overflow CPU/PG/calibration are skipped before evaluation, using the
   costs measured in step 1.
4. **Finish.** Fill slots the objective did not need (EHP/DPS without hurting the
   objective or constraints), re-pick drones (a small knapsack over bandwidth, bay and
   max active) and ammo, then verify with the full capacitor simulation. If averages
   were too optimistic, the local search continues with the simulation on.

Score = `sum(weight * metric / scale)` − 1000 × (relative constraint violation), plus a
tiny tie-breaker that prefers spare CPU/PG and lower price. Feasible fits always win,
infeasible ones are graded by how close they get, and plateaus (an oversized module
that can be swapped for a smaller one with no immediate gain) can be walked off.

Typical run times on one core: quick 2–10 s, normal 20–40 s per hull.

## Ship coverage

`tools/audit_ships.py` computes every published hull (421) on the empty hull and runs a
quick optimization for one hull per ship group (49 groups). All compute without errors,
and no hull has effects the engine cannot execute. What it found and what was done:

* **T3 destroyer modes** were missing (CCP marks them unpublished). They are now in the
  data, required on T3D fits, and chosen by the optimizer. Two Svipul fits match Pyfa.
* **T3 cruiser subsystems** can be chosen automatically (`subsystems = "auto"`), by
  coordinate descent over the four subsystem slots using fast screening fits.
* **Burst modules** (assault damage controls, cloaks, micro jump drives, bombs) have a
  long reactivation delay. They used to count as permanently active, which gave an
  assault frigate 194k EHP. They now default to online.
* **Fighters** (carriers, supercarriers, one dreadnought, one titan) are modelled: see
  [Fighters](#fighters).
* **Roles without damage** (logistics, mining, hauling) now have metrics:
  `remote_repair`, `mining_yield` and `cargo` (all matching Pyfa). When no damage metric
  is in play, high slots stay free for repairers or mining lasers instead of weapons.
* **Freighter oddity:** in this game build the Avalanche has six launcher hardpoints
  while other freighters have none; that comes from CCP's data, not evesim.
* **Storyline modules** include officer-grade "Naiyon's Modified" items. Combined with
  Polarized weapons they produce very high DPS fits (an Eris at 1,412 DPS), which Pyfa
  confirms. Restrict `meta` or set a budget if you want ordinary fits.

## Fighters

A fighter squadron is an item in the fighter bay with an amount (EFT `Templar II x6`).
Hull, skill and Fighter Support Unit / Drone Damage Amplifier bonuses reach fighters
through the normal modifier rules. Fitting checks cover the fighter skills, the total
number of tubes, the light/support/heavy tube limits, the bay volume and each type's
maximum squadron size.

Damage follows Pyfa's defaults: abilities are active in effect-ID order up to and
including the standard attack, except propulsion and evasive abilities. A squadron's
DPS is damage × multiplier × squadron size per cycle. Secondary salvos (missiles,
rockets) are counted like the attack, ignoring their limited charges and rearm time,
and bombs are off. Application uses the missile formula with the fighter's explosion
radius and velocity, and assumes the squadron reaches the target.

The optimizer fills tubes greedily by value per squadron for the objective, using full
squadrons, and leaves out structure (Standup) fighters. All four reference carrier fits
match Pyfa per squadron, including the Drone Damage Amplifier bonus.

## Exploration and harvesting

* **Probes:** `probe_strength` is the scan strength of the strongest probe loaded in a
  launcher, after skills, launcher, rigs, implants and hull bonuses. Core probes only fit
  core launchers' charge groups, combat probes need an expanded launcher. Deviation and
  whether a probe can find ships are reported too. The optimizer loads the strongest
  allowed probe (set `fitting.ammo` to choose combat probes).
* **Hacking:** data and relic analyzers report virus coherence (your hit points in the
  hacking game), virus strength (damage per hit) and utility element slots. Integrated
  analyzers count for both. The combined metrics (`hacking_coherence`, `hacking_strength`,
  `hacking_power` = coherence × strength) take the weaker kind, so a fit must handle both
  site types. Because one analyzer alone then gains nothing, the search scores data and relic
  separately when choosing candidates and gives a little partial credit for one kind (never
  enough to make a fit with only one kind feasible).
* **Salvage:** `salvage_chance` is the salvager's access bonus added to each wreck's base
  chance (it does not grow with more salvagers, which only salvage faster).
* **Harvesting:** ore, ice and gas yields are separate metrics, told apart by the harvester's
  effect and required skills; residue (`mining_waste`) is the extra volume destroyed, as in
  Pyfa's "drain" figure.

Cloaks default to online, like other burst modules, so they do not slow the fit; Pyfa
activates them, which is why the reference fits leave cloaks out.

## Training advisor

For a fixed fit, each skill below V is raised one level (with prerequisites) and the
objective is re-scored; a skill is listed only for what it adds beyond its prerequisites.
Training time is `250 * rank * sqrt(32)^(L-1)` skill points per level at
`primary + secondary / 2` SP per minute. The plan is greedy by gain per day.
Unlock analysis re-optimizes the hull with each candidate skill set (sets that unlock
upgrades of items already in the fit first). Every re-run also yields fits the current
skills can already fly, and the best of those is the reference, so a better search
result is not mistaken for a skill benefit.

## Not modelled (yet)

Command bursts and fleet boosts, projected effects (your webs
and paints on the target are reported as strength and range but do not change your
applied damage; enemy EWAR on you), mutated (abyssal)
modules, drugs, heat damage, Reactive Armor Hardener adaptation, entropic
disintegrator spool-up in the optimizer (the stats report base damage and the
`spool_max` bonus), breacher pods, and drone application beyond "outrun or not". The
optimizer does not choose implants (ESI implants are applied when fitting stats), and
fills utility high slots only when a metric rewards them (EWAR, remote repair, mining).
Mining crystals only boost their own ore type, and `mining_yield` assumes you mine that
ore; set `fitting.ammo` to the crystal for your ore.

## Open decisions

These choices shape what the optimizer recommends and are worth settling:

1. **Which roles to ship presets for.** Example specs exist for PvE, brawling, kiting
   and budget ratting. Abyssals, wormholes, faction warfare or fleet doctrines would
   each want their own target/incoming presets.
2. **Default module pool.** It is currently T1 + T2 + storyline. Faction and deadspace
   are opt-in. Polarized weapons (zero resists) are allowed, and win whenever EHP is
   only a lower bound.
3. **Capacitor stability semantics.** Everything active is assumed running, including
   the prop mod. Some players judge stability with prop off.
4. **Price source.** ESI average prices are free but loose. Jita sell prices would need
   a market API (e.g. Fuzzwork or Janice).
5. **Default module pool for storyline items.** Officer-grade storyline modules are
   included by default; a metaLevel cap may suit most players better.
