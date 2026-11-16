"""What real fits look like: a model trained on killmails and community fits.

For every hull and slot type (high, mid, low, rig, subsystem) the model holds how often
each *module family* fills a slot, empty slots included. A family groups a module with
its meta variants (Damage Control I, II, 'Radical', Shadow Serpentis ... are one family),
so the model learns *what kind* of module goes where, while the optimizer still picks the
best variant your skills and budget allow. Hulls with few recorded fits borrow strength
from their ship group and from all ships (hierarchical smoothing), and the PvE model is
trained on losses to NPCs and curated fits, smoothed towards all fits of the hull.

A fit's *typicality* is the geometric mean, over its slots, of how likely each slot's
choice is compared with the most common module for that slot: 1.0 for a fit made only of
the most common choices, around 0.3-0.7 for an ordinary real fit, near 0 for module
choices nobody uses on that hull. An empty slot scores like a module as common as
"empty" is on that hull, but never better than the most common module, so the model
steers *which* modules are fitted, not whether slots are filled. Fits with most slots
empty (typically new players' losses) are left out of training. The optimizer adds ``practicality * log`` of these ratios to its
score, so among fits with similar objective values it prefers the kind real pilots fly,
and it starts its search from the most common real fits of the hull as well as from
scratch.

The model also keeps the most common complete fits of each hull, used as those starting
points and shown by ``evesim fits SHIP``.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import time
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from evesim.corpus import FLAG_SLOT, SLOT_FLAGS, RealFit
from evesim.data.store import StaticData, Type

SLOTS = ("high", "med", "low", "rig", "subsystem")
EMPTY = 0                  # family id of an empty slot
CURATED_WEIGHT = 10.0      # one curated fit counts like this many killmails
K_GROUP, K_HULL, K_TAG = 40.0, 20.0, 30.0  # smoothing strengths, in slot instances
FLOOR = -4.0               # a slot never costs more than this (log ratio): rare or brand-new modules
                           # stay possible when they are clearly better
TOP_FITS = 6
MIN_FILLED = 0.6           # fits with fewer high/mid/low slots filled (new players' losses) are skipped
TAGS = ("pve", "pvp", "all")


def default_prior_path() -> Path:
    env = os.environ.get("EVESIM_PRIOR")
    if env:
        return Path(env)
    shipped = Path(__file__).resolve().parent / "data" / "prior.json.gz"
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    cached = Path(base) / "evesim" / "prior.json.gz"
    return cached if cached.exists() else shipped


def family(t: Type) -> int:
    """Meta variants share a family: their variation parent (the T1 item). Mutated and other
    unmarketed modules are grouped by item group (negative ids)."""
    if t.variation_parent:
        return int(t.variation_parent)
    if t.market_group is None:
        return -int(t.group_id)
    return t.id


# ---- training ---------------------------------------------------------------------------------

class _SlotCounter:
    def __init__(self):
        self.n = defaultdict(float)                                   # tag -> fits
        self.c = defaultdict(lambda: defaultdict(Counter))            # tag -> slot -> family -> w

    def add(self, tag: str, slot_fams: dict[str, list[int]], w: float):
        self.n[tag] += w
        for slot, fams in slot_fams.items():
            for f in fams:
                self.c[tag][slot][f] += w

    def dump(self, min_count: float) -> dict:
        return {"n": dict(self.n),
                "slots": {tag: {slot: {str(f): round(v, 1) for f, v in cnt.items() if v >= min_count or f == EMPTY}
                                for slot, cnt in slots.items()} for tag, slots in self.c.items()}}


def _slot_counts(data: StaticData, fit: RealFit, totals_cache: dict) -> dict[str, int]:
    ship = data.types[fit.ship]
    subs = tuple(sorted(t for f, t, _ in fit.modules if FLAG_SLOT.get(f) == "subsystem"))
    key = (fit.ship, subs)
    if key not in totals_cache:
        out = {"high": int(ship.attr("hiSlots", 0.0)), "med": int(ship.attr("medSlots", 0.0)),
               "low": int(ship.attr("lowSlots", 0.0)), "rig": int(ship.attr("rigSlots", 0.0)),
               "subsystem": int(ship.attr("maxSubSystems", 0.0))}
        for s in subs:  # T3 cruisers get their slots from subsystems
            t = data.types[s]
            out["high"] += int(t.attr("hiSlotModifier", 0.0))
            out["med"] += int(t.attr("medSlotModifier", 0.0))
            out["low"] += int(t.attr("lowSlotModifier", 0.0))
        totals_cache[key] = out
    return totals_cache[key]


def _tags(fit: RealFit) -> tuple[str, ...]:
    if fit.tag == "curated":
        return ("pve", "all")
    return (fit.tag, "all")


def train(fits: Iterable[RealFit], data: StaticData, log=print) -> dict:
    """Count module families per hull, ship group and overall, and keep each hull's most
    common complete fits."""
    hull: dict[int, _SlotCounter] = defaultdict(_SlotCounter)
    group: dict[int, _SlotCounter] = defaultdict(_SlotCounter)
    glob = _SlotCounter()
    top: dict[tuple, Counter] = defaultdict(Counter)
    charges: dict[tuple, Counter] = defaultdict(Counter)   # (ship, module) -> charge counts
    totals: dict = {}
    n = Counter()
    for fit in fits:
        ship = data.types.get(fit.ship)
        if ship is None:
            continue
        slot_fams: dict[str, list[int]] = {s: [] for s in SLOTS}
        for flag, t, ch in fit.modules:
            ty = data.types.get(t)
            slot = FLAG_SLOT.get(flag)
            if ty is None or slot is None:
                continue
            slot_fams[slot].append(family(ty))
            if ch:
                charges[(fit.ship, t)][ch] += 1
        total = _slot_counts(data, fit, totals)
        core = sum(total[s] for s in ("high", "med", "low"))
        filled = sum(len(slot_fams[s]) for s in ("high", "med", "low"))
        if core and filled < MIN_FILLED * core:
            n["skipped (mostly empty)"] += 1
            continue
        for s in SLOTS:
            slot_fams[s] += [EMPTY] * max(0, total[s] - len(slot_fams[s]))
            if not total[s] and not slot_fams[s]:
                del slot_fams[s]
        w = CURATED_WEIGHT if fit.tag == "curated" else 1.0
        for tag in _tags(fit):
            hull[fit.ship].add(tag, slot_fams, w)
            group[ship.group_id].add(tag, slot_fams, w)
            glob.add(tag, slot_fams, w)
        n[fit.tag] += 1
        # Complete fits, ignoring charges, counted for the "most common fits" list.
        key = (tuple(sorted((FLAG_SLOT.get(f, "?"), t) for f, t, _ in fit.modules)), tuple(sorted(fit.drones)))
        for tag in _tags(fit):
            top[(fit.ship, tag)][key] += w
    tops: dict[str, dict[str, list]] = defaultdict(dict)
    for (ship, tag), cnt in top.items():
        rows = []
        for (mods, drones), c in cnt.most_common(TOP_FITS):
            if c < 2:
                continue
            flags, out_mods = {s: r.start for s, r in SLOT_FLAGS.items()}, []
            for slot, t in mods:
                if slot in flags:
                    out_mods.append([flags[slot], t, None])
                    flags[slot] += 1
            rows.append([round(c, 1), out_mods, [list(d) for d in drones], []])
        if rows:
            tops[str(ship)][tag] = rows
    loads: dict[str, dict[str, int]] = defaultdict(dict)
    for (ship, t), cnt in charges.items():
        if str(ship) in tops:
            ch, c = cnt.most_common(1)[0]
            if c >= 2:
                loads[str(ship)][str(t)] = ch
    log(f"read {sum(n.values())} fits: " + ", ".join(f"{k} {v}" for k, v in n.most_common()))
    return {
        "meta": {"format": 1, "built_at": int(time.time()), "fits": dict(n), "client_build": data.meta.get("client_build")},
        "hull": {str(k): v.dump(2.0) for k, v in hull.items()},
        "group": {str(k): v.dump(3.0) for k, v in group.items()},
        "global": glob.dump(3.0),
        "top": tops,
        "charges": loads,
    }


def save(model: dict, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(model, f, separators=(",", ":"))
    tmp.replace(path)


# ---- scoring ----------------------------------------------------------------------------------

class FitPrior:
    """Smoothed per-slot family distributions, and typicality of whole fits."""

    def __init__(self, raw: dict, data: StaticData):
        self.raw = raw
        self.data = data
        self.meta = raw.get("meta", {})
        self._fam: dict[int, int] = {}

    @classmethod
    def load(cls, data: StaticData, path: str | Path | None = None) -> "FitPrior | None":
        p = Path(path) if path else default_prior_path()
        if not p.exists():
            return None
        opener = gzip.open if p.suffix == ".gz" else open
        with opener(p, "rt", encoding="utf-8") as f:
            return cls(json.load(f), data)

    def family(self, t: Type) -> int:
        f = self._fam.get(t.id)
        if f is None:
            f = self._fam[t.id] = family(t)
        return f

    def fits_seen(self, ship: int, tag: str = "all") -> float:
        return self.raw["hull"].get(str(ship), {}).get("n", {}).get(tag, 0.0)

    @lru_cache(maxsize=4096)
    def table(self, ship: int, slot: str, tag: str) -> tuple[dict[int, float], float, float]:
        """(log-probability per family, log-probability of an unseen family, best log-probability)."""
        def level(node: dict | None, t: str) -> tuple[Counter, float]:
            if not node:
                return Counter(), 0.0
            cnt = node.get("slots", {}).get(t, {}).get(slot, {})
            return Counter({int(k): v for k, v in cnt.items()}), float(sum(cnt.values()))

        g_cnt, g_n = level(self.raw.get("global"), "all")
        vocab = max(len(g_cnt), 1)
        alpha = 0.5
        p = {f: (c + alpha) / (g_n + alpha * vocab) for f, c in g_cnt.items()}
        unseen = alpha / (g_n + alpha * vocab)

        def smooth(prev: dict, prev_unseen: float, cnt: Counter, n: float, k: float):
            if n <= 0:
                return prev, prev_unseen
            out = {f: (cnt.get(f, 0.0) + k * pf) / (n + k) for f, pf in prev.items()}
            for f, c in cnt.items():
                if f not in out:
                    out[f] = (c + k * prev_unseen) / (n + k)
            return out, k * prev_unseen / (n + k)

        ship_t = self.data.types.get(ship)
        grp = self.raw["group"].get(str(ship_t.group_id)) if ship_t else None
        hul = self.raw["hull"].get(str(ship))
        p, unseen = smooth(p, unseen, *level(grp, "all"), K_GROUP)
        p, unseen = smooth(p, unseen, *level(hul, "all"), K_HULL)
        if tag != "all":
            # Activity-specific counts on top: the group's, then the hull's.
            p, unseen = smooth(p, unseen, *level(grp, tag), K_TAG * 2)
            p, unseen = smooth(p, unseen, *level(hul, tag), K_TAG)
        logp = {f: math.log(v) for f, v in p.items() if v > 0}
        # Compare with the most common *module*: leaving a slot empty is never better than that.
        best = max((v for f, v in logp.items() if f != EMPTY), default=0.0)
        return logp, math.log(unseen) if unseen > 0 else -30.0, best

    def slot_scores(self, ship: int, slots: dict[str, list[Type | None]], totals: dict[str, int],
                    tag: str = "pve") -> dict[str, float]:
        """Sum over each slot type of log(p(choice) / p(most common choice)); empty slots
        count as the 'empty' choice."""
        out = {}
        for slot in SLOTS:
            n = totals.get(slot, 0)
            mods = [t for t in slots.get(slot, []) if t is not None]
            if not n and not mods:
                continue
            logp, unseen, best = self.table(ship, slot, tag)
            s = 0.0
            for t in mods:
                s += max(logp.get(self.family(t), unseen) - best, FLOOR)
            s += max(0, n - len(mods)) * min(max(logp.get(EMPTY, unseen) - best, FLOOR), 0.0)
            out[slot] = s
        return out

    def score_fit(self, fit, totals: dict[str, int], tag: str = "pve") -> tuple[float, int]:
        """(sum of slot log-ratios, number of slots) for an evesim Fit."""
        slots: dict[str, list[Type]] = {}
        for m in fit.modules:
            slots.setdefault(m.slot, []).append(m.type)
        parts = self.slot_scores(fit.ship.id, slots, totals, tag)
        n = sum(max(totals.get(s, 0), len(slots.get(s, []))) for s in parts)
        return sum(parts.values()), n

    def typicality(self, fit, totals: dict[str, int], tag: str = "pve") -> float:
        s, n = self.score_fit(fit, totals, tag)
        return math.exp(s / n) if n else 1.0

    def top_fits(self, ship: int, tag: str = "pve") -> list[RealFit]:
        rows = self.raw["top"].get(str(ship), {})
        loads = self.raw.get("charges", {}).get(str(ship), {})
        out, seen = [], set()
        for t in (tag, "all") if tag != "all" else ("all",):
            for count, mods, drones, fighters in rows.get(t, []):
                key = json.dumps(mods)
                if key in seen:
                    continue
                seen.add(key)
                mods = [(flag, t_id, ch or loads.get(str(t_id))) for flag, t_id, ch in mods]
                f = RealFit(ship, mods, [tuple(d) for d in drones], [tuple(x) for x in fighters],
                            t, f"{count:g} seen")
                out.append(f)
        return out


_cache: dict[str, FitPrior | None] = {}


def get_prior(data: StaticData, path: str | Path | None = None) -> FitPrior | None:
    key = f"{id(data)}:{path or default_prior_path()}"
    if key not in _cache:
        _cache[key] = FitPrior.load(data, path)
    return _cache[key]
