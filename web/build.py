"""Assemble the browser demo into a directory of static files.

    python web/build.py OUT_DIR --pyodide node_modules/pyodide [--data staticdata.json.gz]
                        [--images PYFA/imgs]

Pyodide comes from the npm package (``npm install pyodide@0.27.7``); its runtime is
served next to the page. The example readouts shown before Python finishes loading
are computed here with the same code.

``--images`` points at Pyfa's ``imgs`` directory (module icons by iconID, ship renders by
graphicID). They are packed into two atlas images for the fitting window; this needs
Pillow. Without it the page shows lettered tiles instead of pictures.
"""

import argparse
import base64
import gzip
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
PYODIDE_FILES = ["pyodide.js", "pyodide.asm.js", "pyodide.asm.wasm", "pyodide-lock.json"]
SAMPLE_NAMES = {
    "caracal": "Caracal: heavy missiles", "rifter": "Rifter: autocannons", "vexor": "Vexor: drones and blasters",
    "punisher": "Punisher: pulse lasers", "myrmidon_aar": "Myrmidon: ancillary armor repairer",
    "loki": "Loki: T3 cruiser", "raven_cruise": "Raven: cruise missiles", "astero_explo": "Astero: exploration",
    "skiff_ice": "Skiff: ice mining", "venture_gas": "Venture: gas huffing", "thanatos_fighters": "Thanatos: fighters",
}
DEFAULT_OPT = {
    "skills": "all5",
    "spec": {"ship": "Caracal", "objective": {"weights": {"applied_dps": 1}},
             "target": {"preset": "cruiser", "distance": 30000}, "incoming": {"preset": "guristas"},
             "constraints": {"min_ehp": 12000, "cap_stable": True},
             "fitting": {"tank": "shield", "propulsion": "ab", "meta": ["tech1", "tech2", "storyline"],
                         "require": [], "exclude": [], "subsystems": "auto"},
             "search": {"effort": "quick"}},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--pyodide", required=True, help="path to the pyodide npm package")
    ap.add_argument("--data", help="static data file (default: evesim's cache)")
    ap.add_argument("--images", help="Pyfa's imgs directory, for icons and ship renders")
    a = ap.parse_args()
    out = Path(a.out)
    (out / "pyodide").mkdir(parents=True, exist_ok=True)

    sys.path[:0] = [str(SRC), str(HERE)]
    from evesim.data.store import default_data_path
    import webapi

    data_path = Path(a.data) if a.data else default_data_path()
    # Artifact hosting serves no archives, so ship the data as plain JSON and the Python
    # standard library zip as base64 text (worker.js decodes it).
    with gzip.open(data_path, "rb") as f:
        (out / "staticdata.json").write_bytes(f.read())
    for f in PYODIDE_FILES:
        shutil.copy(Path(a.pyodide) / f, out / "pyodide" / f)
    stdlib = (Path(a.pyodide) / "python_stdlib.zip").read_bytes()
    (out / "pyodide" / "python_stdlib.b64.txt").write_text(base64.b64encode(stdlib).decode())
    shutil.copy(HERE / "worker.js", out / "worker.js")

    files = {str(p.relative_to(SRC)): p.read_text() for p in SRC.rglob("*.py")}
    files["webapi.py"] = (HERE / "webapi.py").read_text()
    (out / "evesim_src.json").write_text(json.dumps(files))

    ref = json.loads((HERE.parent / "tests" / "data" / "pyfa_reference.json").read_text())["fits"]
    samples = {SAMPLE_NAMES.get(k, k): ref[k] for k in SAMPLE_NAMES if k in ref}
    webapi.init(str(data_path))
    if a.images:
        pack_images(Path(a.images), webapi._data, out)
    first = next(iter(samples.values()))
    (out / "catalog.json").write_text(webapi.catalog())
    initial_fit = json.loads(webapi.parse_eft(json.dumps({"eft": first})))
    initial_ship = json.loads(webapi.ship_info(json.dumps({"ship": initial_fit["ship"], "skills": "all5"})))
    initial = json.loads(webapi.simulate_fit(json.dumps(dict(initial_fit, skills="all5", profile="uniform",
                                                             target={"preset": "cruiser", "distance": 20000}))))
    initial_opt = json.loads(webapi.optimize(json.dumps(DEFAULT_OPT), lambda m: None))

    html = (HERE / "index.html").read_text()
    html = html.replace(html[html.index("const SAMPLES = "):html.index(";\nconst INITIAL = ")],
                        "const SAMPLES = " + json.dumps(samples))
    html = html.replace("const INITIAL = null;", "const INITIAL = " + json.dumps(initial) + ";")
    html = html.replace("const INITIAL_FIT = null;", "const INITIAL_FIT = " + json.dumps(initial_fit) + ";")
    html = html.replace("const INITIAL_SHIP = null;", "const INITIAL_SHIP = " + json.dumps(initial_ship) + ";")
    html = html.replace("const INITIAL_OPT = null;", "const INITIAL_OPT = " + json.dumps(initial_opt) + ";")
    from evesim.optimize.search import SHIP_CLASSES
    from evesim.optimize.spec import METRICS
    info = {m.name: {"unit": m.unit, "higher": m.higher_is_better, "about": m.description} for m in METRICS.values()}
    html = html.replace("const METRICS_INFO = null;", "const METRICS_INFO = " + json.dumps(info) + ";")
    html = html.replace(html[html.index("const SHIP_CLASSES = "):html.index(";\nconst label = ")],
                        "const SHIP_CLASSES = " + json.dumps(list(SHIP_CLASSES)))
    (out / "index.html").write_text(html)
    print(f"built {out}")


def _atlas(paths: dict, size: int, mode: str):
    from PIL import Image
    ids = sorted(paths)
    cols = 32
    rows = (len(ids) + cols - 1) // cols
    sheet = Image.new(mode, (cols * size, max(rows, 1) * size), (0, 0, 0, 0) if mode == "RGBA" else (0, 0, 0))
    index = {}
    for i, k in enumerate(ids):
        im = Image.open(paths[k]).convert(mode)
        if im.size != (size, size):
            im = im.resize((size, size), Image.LANCZOS)
        sheet.paste(im, ((i % cols) * size, (i // cols) * size))
        index[k] = i
    return sheet, {"cols": cols, "rows": rows, "size": size, "ids": index}


def pack_images(imgs: Path, data, out: Path):
    """Icon and render atlases plus their index (images.json)."""
    from evesim.data.store import CAT_CHARGE, CAT_DRONE, CAT_FIGHTER, CAT_MODULE, CAT_SHIP, CAT_SUBSYSTEM

    def pick(folder, ident):
        for name in (f"{ident}@2x.png", f"{ident}@1x.png", f"{ident}.png"):
            p = imgs / folder / name
            if p.exists():
                return p
        return None

    icon_ids, render_ids = set(), set()
    for t in data.types.values():
        if not t.market_group:
            continue
        if t.category_id == CAT_SHIP and t.graphic_id:
            render_ids.add(t.graphic_id)
        elif t.category_id in (CAT_MODULE, CAT_SUBSYSTEM, CAT_CHARGE, CAT_DRONE, CAT_FIGHTER) and t.icon_id:
            icon_ids.add(t.icon_id)
    icon_ids.update(icon for _, _, icon in data.market_groups.values() if icon)
    icons = {str(i): p for i in icon_ids if (p := pick("icons", i))}
    renders = {str(i): p for i in render_ids if (p := pick("renders", i))}
    sheet, icon_index = _atlas(icons, 32, "RGBA")
    sheet.save(out / "icons.png", optimize=True)
    sheet, render_index = _atlas(renders, 64, "RGB")
    sheet.save(out / "renders.jpg", quality=88)
    (out / "images.json").write_text(json.dumps({"icon": icon_index, "render": render_index}))
    print(f"packed {len(icons)} icons and {len(renders)} ship renders")


if __name__ == "__main__":
    main()
