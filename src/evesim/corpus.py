"""Real fits, for learning what practical fits look like.

Two sources:

* **Killmails.** EVE Ref publishes every day's ESI killmails as one archive
  (``data.everef.net/killmails``). Each victim's fitted modules, loaded charges,
  drones and fighters are kept. A loss to NPCs only is tagged ``pve``, unless CONCORD or
  an empire faction's police or navy took part (gankers and outlaws, who fly PvP fits);
  everything else is ``pvp``.
* **Curated fits.** Fittings published on the EVE University wiki with its
  ``ShipFitting`` template (incursion doctrines, exploration, travel, ...), tagged
  ``curated``.

Records are stored as gzipped JSON lines in ``~/.cache/evesim/corpus`` (one file per
killmail day, one for the wiki), so a later ``fetch`` only downloads new days.
A record is ``[ship, [[flag, type, charge], ...], [[drone, n], ...], [[fighter, n], ...],
tag, source_id]``, using the in-game inventory flags for slots.
"""

from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import os
import re
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from evesim.data.store import CAT_CHARGE, CAT_DRONE, CAT_FIGHTER, CAT_MODULE, CAT_SHIP, CAT_SUBSYSTEM, StaticData

EVEREF_DAY = "https://data.everef.net/killmails/{y}/killmails-{d}.tar.bz2"
UNIWIKI_API = "https://wiki.eveuniversity.org/api.php"
USER_AGENT = "evesim fit research (https://github.com/HarryZ4127/eve_ship_sim)"

SLOT_FLAGS = {"low": range(11, 19), "med": range(19, 27), "high": range(27, 35), "rig": range(92, 100),
              "subsystem": range(125, 133)}
FLAG_SLOT = {f: s for s, r in SLOT_FLAGS.items() for f in r}
DRONE_BAY, FIGHTER_TUBES = 87, range(159, 164)
GROUP_CAPSULE = 29
CORP_CONCORD = 1000125
LAW_FACTIONS = {500001, 500002, 500003, 500004, 500006}  # Caldari, Minmatar, Amarr, Gallente, CONCORD


def corpus_dir() -> Path:
    env = os.environ.get("EVESIM_CORPUS")
    if env:
        return Path(env)
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "evesim" / "corpus"


@dataclass
class RealFit:
    ship: int
    modules: list[tuple[int, int, int | None]] = field(default_factory=list)  # (flag, type, charge)
    drones: list[tuple[int, int]] = field(default_factory=list)
    fighters: list[tuple[int, int]] = field(default_factory=list)
    tag: str = "pvp"          # pve, pvp or curated
    source: str = ""          # killmail id or wiki page

    def slot_items(self, slot: str) -> list[int]:
        return [t for f, t, _ in self.modules if FLAG_SLOT.get(f) == slot]

    def to_record(self) -> list:
        return [self.ship, [list(m) for m in self.modules], [list(d) for d in self.drones],
                [list(x) for x in self.fighters], self.tag, self.source]

    @classmethod
    def from_record(cls, r: list) -> "RealFit":
        return cls(r[0], [tuple(m) for m in r[1]], [tuple(d) for d in r[2]], [tuple(x) for x in r[3]], r[4], r[5])

    def eft(self, data: StaticData, name: str = "") -> str:
        """EFT text, slot by slot (empty slots are left out)."""
        T = data.types
        lines = [f"[{T[self.ship].name}, {name or self.tag + ' ' + str(self.source)}]"]
        for slot in ("low", "med", "high", "rig", "subsystem"):
            mods = sorted((f, t, c) for f, t, c in self.modules if FLAG_SLOT.get(f) == slot)
            if not mods and slot == "subsystem":
                continue
            for _, t, c in mods:
                lines.append(T[t].name + (f", {T[c].name}" if c else ""))
            lines.append("")
        for t, n in self.drones:
            lines.append(f"{T[t].name} x{n}")
        if self.fighters:
            lines.append("")
            for t, n in self.fighters:
                lines.append(f"{T[t].name} x{n}")
        return "\n".join(lines).rstrip() + "\n"


# ---- killmails -----------------------------------------------------------------------------------

def fit_from_killmail(km: dict, data: StaticData) -> RealFit | None:
    """The victim's fit, or None when the victim is not a ship or has nothing fitted."""
    v = km.get("victim") or {}
    ship = data.types.get(v.get("ship_type_id"))
    if ship is None or ship.category_id != CAT_SHIP or ship.group_id == GROUP_CAPSULE:
        return None
    mods: dict[int, int] = {}
    charges: dict[int, int] = {}
    drones: dict[int, int] = {}
    fighters: list[tuple[int, int]] = []
    for it in v.get("items") or []:
        flag = it.get("flag")
        t = data.types.get(it.get("item_type_id"))
        if t is None:
            continue
        qty = int(it.get("quantity_destroyed", 0)) + int(it.get("quantity_dropped", 0))
        if flag in FLAG_SLOT:
            if t.category_id in (CAT_MODULE, CAT_SUBSYSTEM):
                mods[flag] = t.id
            elif t.category_id == CAT_CHARGE:
                charges[flag] = t.id
        elif flag == DRONE_BAY and t.category_id == CAT_DRONE:
            drones[t.id] = drones.get(t.id, 0) + qty
        elif flag in FIGHTER_TUBES and t.category_id == CAT_FIGHTER:
            fighters.append((t.id, qty))
    if not mods:
        return None
    attackers = km.get("attackers") or []
    players = any("character_id" in a for a in attackers)
    law = any(a.get("corporation_id") == CORP_CONCORD or a.get("faction_id") in LAW_FACTIONS for a in attackers)
    return RealFit(ship.id, sorted((f, t, charges.get(f)) for f, t in mods.items()),
                   sorted(drones.items()), fighters, "pvp" if players or law else "pve", str(km.get("killmail_id", "")))


def _download(url: str, timeout: float = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError:
            raise
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def killmail_day(day: dt.date, data: StaticData) -> list[RealFit]:
    """Download and parse one day's killmails (the archive is read in memory, not unpacked)."""
    raw = _download(EVEREF_DAY.format(y=day.year, d=day.isoformat()))
    out = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:bz2") as tf:
        for m in tf:
            if not m.isfile() or not m.name.endswith(".json"):
                continue
            try:
                km = json.load(tf.extractfile(m))
            except (ValueError, UnicodeDecodeError):
                continue
            f = fit_from_killmail(km, data)
            if f is not None:
                out.append(f)
    return out


def fetch_killmails(data: StaticData, days: int = 30, end: dt.date | None = None,
                    log: Callable[[str], None] = print, refresh: bool = False) -> int:
    """Make sure the last ``days`` days of killmail fits are cached. EVE Ref publishes a
    day's archive a little after midnight UTC, so the default ends two days ago."""
    d = corpus_dir()
    d.mkdir(parents=True, exist_ok=True)
    end = end or (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=2))
    total = 0
    for i in range(days):
        day = end - dt.timedelta(days=i)
        path = d / f"km-{day.isoformat()}.jsonl.gz"
        if path.exists() and not refresh:
            continue
        try:
            fits = killmail_day(day, data)
        except urllib.error.HTTPError as e:
            log(f"  {day}: not available ({e.code})")
            continue
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            for x in fits:
                f.write(json.dumps(x.to_record(), separators=(",", ":")) + "\n")
        tmp.replace(path)
        total += len(fits)
        log(f"  {day}: {len(fits)} fits")
    return total


# ---- EVE University wiki ------------------------------------------------------------------------

def _wiki(params: dict) -> dict:
    q = urllib.parse.urlencode(dict(params, format="json"))
    return json.loads(_download(f"{UNIWIKI_API}?{q}", timeout=60))


_FIELD = re.compile(r"^\s*\|\s*([A-Za-z]+)(\d*)(name|typeID)?\s*=\s*(.*?)\s*$")


def parse_shipfitting(block: str, data: StaticData, source: str) -> RealFit | None:
    """One ``{{ShipFitting ...}}`` template: ship, high1name, mid1name, low1name, rig1name,
    subsystem1name, drone1name ("Hammerhead II x5"), charge1name."""
    fields: dict[str, str] = {}
    slots: dict[str, dict[int, str]] = {}
    for line in block.splitlines():
        m = _FIELD.match(line)
        if not m:
            continue
        key, num, kind, value = m.group(1).lower(), m.group(2), m.group(3), m.group(4)
        if num and kind == "name":
            slots.setdefault(key, {})[int(num)] = value
        elif not num and not kind:
            fields[key] = value

    def lookup(name: str):
        name = re.sub(r"\s+x\d+$", "", name.strip())
        try:
            return data.type_by_name(name)
        except KeyError:
            return None

    ship = lookup(fields.get("ship", ""))
    if ship is None or ship.category_id != CAT_SHIP:
        return None
    base = {"low": 11, "mid": 19, "high": 27, "rig": 92, "subsystem": 125}
    mods, charges = [], {}
    for key, flag0 in base.items():
        for i, name in sorted(slots.get(key, {}).items()):
            t = lookup(name)
            if t is not None and t.category_id in (CAT_MODULE, CAT_SUBSYSTEM):
                mods.append((flag0 + i - 1, t.id, None))
    # Charges are listed once per fit; load each into every module that takes it.
    from evesim.fit import charge_fits
    loaded = [lookup(n) for _, n in sorted(slots.get("charge", {}).items())]
    loaded = [c for c in loaded if c is not None and c.category_id == CAT_CHARGE]
    for k, (flag, t, _) in enumerate(mods):
        ch = next((c for c in loaded if charge_fits(data.types[t], c)), None)
        if ch is not None:
            mods[k] = (flag, t, ch.id)
    drones = []
    for _, name in sorted(slots.get("drone", {}).items()):
        t, n = lookup(name), re.search(r"x(\d+)\s*$", name)
        if t is not None and t.category_id in (CAT_DRONE, CAT_FIGHTER):
            drones.append((t.id, int(n.group(1)) if n else 1))
    if not mods:
        return None
    return RealFit(ship.id, mods, [d for d in drones if data.types[d[0]].category_id == CAT_DRONE],
                   [d for d in drones if data.types[d[0]].category_id == CAT_FIGHTER], "curated", source)


def fetch_uniwiki(data: StaticData, log: Callable[[str], None] = print, pause: float = 0.5) -> int:
    pages, cont = [], {}
    while True:
        r = _wiki(dict({"action": "query", "list": "embeddedin", "eititle": "Template:ShipFitting",
                        "eilimit": 500}, **cont))
        pages += [p["title"] for p in r["query"]["embeddedin"]]
        if "continue" not in r:
            break
        cont = r["continue"]
    fits = []
    for title in pages:
        if title.startswith("Template:"):
            continue
        try:
            text = _wiki({"action": "parse", "page": title, "prop": "wikitext"})["parse"]["wikitext"]["*"]
        except Exception as e:  # a page that fails to load is skipped, not fatal
            log(f"  {title}: {e}")
            continue
        for block in re.findall(r"\{\{ShipFitting(.*?)\n\}\}", text, flags=re.S):
            f = parse_shipfitting(block, data, title)
            if f is not None:
                fits.append(f)
        time.sleep(pause)
    path = corpus_dir() / "uniwiki.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for x in fits:
            f.write(json.dumps(x.to_record(), separators=(",", ":")) + "\n")
    log(f"  EVE University wiki: {len(fits)} fits from {len(pages)} pages")
    return len(fits)


def load(path: Path | None = None) -> Iterator[RealFit]:
    """Every cached fit."""
    d = path or corpus_dir()
    for p in sorted(d.glob("*.jsonl.gz")):
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                yield RealFit.from_record(json.loads(line))
