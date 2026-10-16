# Requirements spec reference

A spec is a TOML file passed to `evesim optimize`. Every section is optional except
the ship. Unknown sections, metrics, presets or values are rejected with an error,
not silently ignored.

## How to choose requirements

Requirements trade three things off:

* **Usefulness.** Would you actually fly the result? Constrain what makes a fit unusable
  for the job (dies too fast, runs out of cap, too slow to catch anything), and
  *maximize* the one or two things you want more of.
* **Specificness.** The more precisely you describe the engagement (target size and
  speed, range, incoming damage type), the better the answer for that engagement and
  the worse it may be for others. A Caracal tuned for cruisers at 30 km is a different
  fit from one tuned for frigates at 10 km.
* **Practicability.** Every constraint you add narrows the search. Infeasible specs
  are reported as such, with the best compromise and which constraints failed. Keep
  budgets and meta limits realistic for what you can buy.

A good pattern is **one objective, two to four constraints, one or two fitting
choices**:

| role | objective | constraints | fitting |
|---|---|---|---|
| PvE missions / anomalies | `applied_dps` vs the NPC class you fight | `cap_stable`, `min_sustained_tank` vs the faction profile | `tank`, `propulsion = "ab"` |
| Solo brawler | weights `applied_dps` + `ehp` | `cap_seconds >= 120` | `require` scram/web, `propulsion = "mwd"` |
| Kiter | weights `applied_dps` + `speed` | `min_weapon_range`, `min_speed` | `propulsion = "mwd"` |
| Fleet / alpha | `volley` or `ehp` | `min_lock_range`, `max_align_time` | `meta` limits for doctrine |
| Travel / hauler | `ehp` | `max_align_time` | `propulsion = "mwd"` |

## Fields

### Ship

```toml
ship = "Caracal"            # one hull
ships = ["Kestrel", "Condor"]
ship_groups = ["Frigate"]   # every hull in the group your skills can fly (slow for big groups)
```

Several hulls are optimized one after another and ranked by score.

### `[objective]`

```toml
maximize = "applied_dps"
# or
weights = { applied_dps = 1.0, ehp = 0.5 }
# optional: scales = { ehp = 20000 }  (see below)
```

The score is `sum(weight * metric / scale)`. Scales put metrics on comparable footing.
With weight 1, one "scale unit" is worth one point:

| metric | scale | metric | scale |
|---|---|---|---|
| dps, applied_dps | 100 HP/s | ehp | 10,000 HP |
| sustained_tank, peak_tank | 100 EHP/s | speed | 1,000 m/s |
| align_time | 10 s | signature | 100 m |
| lock_range | 50 km | weapon_range | 10 km |
| cap_seconds | 300 s | price | 100M ISK |

So `weights = { applied_dps = 1.0, ehp = 0.5 }` trades 100 DPS for 20,000 EHP. Metrics
where lower is better (align time, signature, price) are subtracted automatically.

### `[target]`: who you shoot

```toml
preset = "cruiser"          # frigate, destroyer, cruiser, battlecruiser, battleship, structure
signature = 125             # m
velocity = 250              # m/s
distance = 20000            # m
transversal_share = 1.0     # 1 = orbiting you, 0 = flying straight at you
resists = { kinetic = 0.4 } # resist fractions; omitted types are 0
```

Turret application uses the chance-to-hit formula (tracking and range, wrecking shots
included). Missiles use the explosion formula and flight range. Drones are assumed to
apply fully unless the target outruns them.

### `[incoming]`: who shoots you

```toml
preset = "guristas"         # uniform, em, thermal, kinetic, explosive, guristas, serpentis,
                            # blood, sansha, angel, drones, mordus, triglavian, edencom
# or
em = 0
thermal = 45
kinetic = 55
explosive = 0
```

The NPC presets are approximate damage mixes. Use your own numbers for specific sites.

### `[constraints]`

Any metric can be bounded with `min_<metric>` or `max_<metric>`. Two shortcuts:

```toml
min_ehp = 15000
max_align_time = 6
cap_stable = true           # capacitor never runs dry (time-domain simulation)
budget = 50_000_000         # same as max_price
```

Prices come from ESI's public average prices, refreshed daily. Without network access,
static base prices are used and budgets become unreliable (a warning is printed).

### `[fitting]`

| key | default | meaning |
|---|---|---|
| `meta` | `["tech1","tech2","storyline"]` | allowed module meta groups: `tech1 tech2 storyline faction officer deadspace tech3` |
| `ammo_meta` | adds `faction` | allowed charge meta groups (faction ammo is cheap) |
| `tank` | `any` | `shield` / `armor` / `hull`: drop modules that only help the other layers |
| `propulsion` | `any` | `mwd` / `ab` reserve a mid slot for one; `none` forbids them |
| `oversized_prop` | `false` | allow props sized for bigger hulls (e.g. 100MN AB on a cruiser) |
| `weapons` | `any` | `turret` / `missile` / `drone` / `none` |
| `weapon` | none | pin one weapon module by name |
| `ammo` | all allowed | list of allowed charges by name |
| `drones` | `true` | fill the drone bay |
| `require` | `[]` | modules that must be fitted (kept fixed) |
| `exclude` | `[]` | modules never to use |
| `subsystems` | `[]` | the four subsystems of a T3 cruiser (required for T3Cs) |
| `smartbombs` | `false` | allow smartbombs as utility (they also hit your own drones) |
| `fill_slots` | `true` | fill slots the objective leaves empty with EHP/DPS |
| `cpu_margin`, `pg_margin` | 0 | keep this much CPU/PG free |

### `[search]`

```toml
effort = "normal"           # quick (~5-10 s per hull), normal (~20-40 s), thorough (minutes)
seed = 1                    # the search is randomized but reproducible for a given seed
```

## Reading the result

* **FEASIBLE / INFEASIBLE.** An infeasible result is the best compromise found. Its
  constraint list shows which ones fail and by how much.
* **(binding)** marks a constraint the result sits right at. Relaxing it is where more
  objective comes from.
* **Other weapon setups tried** shows the runner-up weapon/ammo choices and their
  scores, a quick way to see how much the weapon choice matters.
