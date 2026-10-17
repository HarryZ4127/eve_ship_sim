"""Build the compact static-data file evesim runs on.

The source is the FSD JSON dump that Pyfa keeps in its repository
(``staticdata/fsd_built``). It is refreshed every EVE patch, it already carries
dogma ``modifierInfo``, and it is reachable from places where CCP's own
endpoints are not. The builder keeps only what fitting needs (ships, modules,
charges, drones, implants, subsystems, skills and the character type) and only
English names, which shrinks ~200 MB of JSON to a few MB.
"""

from __future__ import annotations

import gzip
import http.client
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

PYFA_RAW = "https://raw.githubusercontent.com/pyfa-org/Pyfa/{ref}/staticdata/"

# Categories that matter for fitting.
KEEP_CATEGORIES = {6, 7, 8, 16, 18, 20, 32}
CHARACTER_TYPE_ID = 1373
GROUP_SHIP_MODES = 1306  # T3 destroyer tactical modes: unpublished, but part of every T3D fit

# Type fields that dogma also exposes as attributes.
TYPE_FIELD_ATTRS = {"mass": 4, "capacity": 38, "volume": 161, "radius": 162}

Loader = Callable[[str], Any]


def _http_loader(ref: str) -> Loader:
    base = PYFA_RAW.format(ref=ref)

    def load(rel: str, attempts: int = 4) -> Any:
        url = base + rel
        req = urllib.request.Request(url, headers={"User-Agent": "evesim (github.com/HarryZ4127/eve_ship_sim)"})
        for i in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError:
                raise  # 404 ends a sharded file list; other statuses are not transient
            except (http.client.IncompleteRead, urllib.error.URLError, ConnectionError, TimeoutError) as e:
                if i == attempts - 1:
                    raise RuntimeError(f"download of {url} failed: {e}") from e
                time.sleep(2 ** (i + 1))

    return load


def _dir_loader(root: Path) -> Loader:
    root = Path(root)
    if (root / "staticdata").is_dir():
        root = root / "staticdata"

    def load(rel: str) -> Any:
        p = root / rel
        if not p.exists():
            raise FileNotFoundError(p)
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    return load


def _load_sharded(load: Loader, stem: str) -> dict:
    """Load ``stem.0.json``, ``stem.1.json`` ... until one is missing."""
    out: dict = {}
    i = 0
    while True:
        try:
            part = load(f"{stem}.{i}.json")
        except (FileNotFoundError, urllib.error.HTTPError) as e:
            if i == 0:
                raise RuntimeError(f"could not load {stem}.0.json: {e}") from e
            break
        out.update(part)
        i += 1
    return out


def _en(v: Any) -> str:
    if isinstance(v, dict):
        return v.get("en-us") or v.get("en") or next(iter(v.values()), "")
    return v or ""


def build(source: str | Path | None = None, ref: str = "master",
          log: Callable[[str], None] = print) -> dict:
    """Return the compact data dict. ``source`` is a local Pyfa checkout (or its
    ``staticdata`` dir); when omitted the files are downloaded from GitHub."""
    load = _dir_loader(Path(source)) if source else _http_loader(ref)
    fsd = "fsd_built/"

    log("loading groups, categories, attributes, effects")
    groups = load(fsd + "groups.0.json")
    categories = load(fsd + "categories.0.json")
    attrs = load(fsd + "dogmaattributes.0.json")
    effects = load(fsd + "dogmaeffects.0.json")
    log("loading types (large)")
    types = _load_sharded(load, fsd + "types")
    log("loading type dogma")
    typedogma = _load_sharded(load, fsd + "typedogma")
    try:
        meta = {r["field_name"]: r["field_value"] for r in load("phobos/metadata.0.json")}
    except Exception:
        meta = {}
    try:
        clonegrades = load("fsd_lite/clonegrades.0.json")
    except Exception:
        clonegrades = {}

    keep_groups = {int(g): v for g, v in groups.items() if v.get("categoryID") in KEEP_CATEGORIES or int(g) == 1}

    out_types: dict[str, dict] = {}
    used_effects: set[int] = set()
    used_attrs: set[int] = set(TYPE_FIELD_ATTRS.values())
    for tid, t in types.items():
        itid = int(tid)
        gid = t.get("groupID")
        if itid != CHARACTER_TYPE_ID:
            if gid not in keep_groups or int(gid) == 1:
                continue
            if not t.get("published") and gid != GROUP_SHIP_MODES:
                continue
        td = typedogma.get(tid, {})
        a = {str(x["attributeID"]): x["value"] for x in td.get("dogmaAttributes", [])}
        for field, aid in TYPE_FIELD_ATTRS.items():
            if str(aid) not in a and t.get(field) is not None:
                a[str(aid)] = t[field]
        effs = [x["effectID"] for x in td.get("dogmaEffects", [])]
        default_eff = next((x["effectID"] for x in td.get("dogmaEffects", []) if x.get("isDefault")), None)
        used_effects.update(effs)
        used_attrs.update(int(k) for k in a)
        rec = {"n": _en(t.get("typeName")), "g": gid, "a": a, "e": effs}
        if default_eff is not None:
            rec["de"] = default_eff
        for src, dst in (("marketGroupID", "mg"), ("metaGroupID", "mt"), ("variationParentTypeID", "vp"),
                         ("basePrice", "p"), ("raceID", "race"), ("factionID", "fac")):
            if t.get(src) is not None:
                rec[dst] = t[src]
        out_types[tid] = rec

    out_effects: dict[str, dict] = {}
    for eid in used_effects:
        e = effects.get(str(eid))
        if not e:
            continue
        mods = []
        for m in e.get("modifierInfo") or []:
            mods.append([m.get("func"), m.get("domain"), m.get("modifiedAttributeID"), m.get("modifyingAttributeID"),
                         m.get("operation"), m.get("groupID"), m.get("skillTypeID"), m.get("effectID")])
            for k in ("modifiedAttributeID", "modifyingAttributeID"):
                if m.get(k) is not None:
                    used_attrs.add(m[k])
        rec = {"n": e.get("effectName", ""), "c": e.get("effectCategory", 0), "m": mods}
        for src, dst in (("durationAttributeID", "dur"), ("dischargeAttributeID", "dis"), ("rangeAttributeID", "rng"),
                         ("falloffAttributeID", "fal"), ("trackingSpeedAttributeID", "trk")):
            if e.get(src):
                rec[dst] = e[src]
                used_attrs.add(e[src])
        out_effects[str(eid)] = rec

    # Capping attributes must be present too.
    for aid in list(used_attrs):
        a = attrs.get(str(aid))
        if a:
            for k in ("maxAttributeID", "minAttributeID"):
                if a.get(k):
                    used_attrs.add(a[k])

    # Keep every attribute: unknown attribute names are needed to look up
    # values by name from the API, and the table is small.
    out_attrs = {}
    for aid, a in attrs.items():
        out_attrs[aid] = [a.get("name", ""), a.get("defaultValue", 0.0), a.get("stackable", 1),
                          a.get("highIsGood", 1), a.get("maxAttributeID"), a.get("minAttributeID"),
                          _en(a.get("displayName")), a.get("unitID")]

    out_groups = {str(g): [_en(v.get("groupName") or v.get("name")), v.get("categoryID")] for g, v in keep_groups.items()}
    out_cats = {k: _en(v.get("categoryName") or v.get("name")) for k, v in categories.items() if int(k) in KEEP_CATEGORIES | {1}}

    grades = {}
    for gid, g in clonegrades.items():
        grades[gid] = {"name": g.get("internalDescription", gid),
                       "skills": {str(s["typeID"]): s["level"] for s in g.get("skills", [])}}

    return {
        "meta": {
            "format": 1,
            "source": str(source) if source else f"github:pyfa-org/Pyfa@{ref}",
            "client_build": meta.get("client_build"),
            "dump_time": meta.get("dump_time"),
            "built_at": int(time.time()),
        },
        "attributes": out_attrs,
        "effects": out_effects,
        "groups": out_groups,
        "categories": out_cats,
        "types": out_types,
        "clone_grades": grades,
    }


def write(data: dict, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    tmp.replace(path)
    return path
