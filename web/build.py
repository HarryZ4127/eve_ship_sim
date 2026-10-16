"""Assemble the browser demo into a directory of static files.

    python web/build.py OUT_DIR --pyodide node_modules/pyodide [--data staticdata.json.gz]

Pyodide comes from the npm package (``npm install pyodide@0.27.7``); its runtime is
served next to the page. The example readouts shown before Python finishes loading
are computed here with the same code.
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
    "loki": "Loki: T3 cruiser", "raven_cruise": "Raven: cruise missiles",
}
DEFAULT_OPT = {
    "skills": "all5",
    "spec": {"ship": "Caracal", "objective": {"weights": {"applied_dps": 1}},
             "target": {"preset": "cruiser", "distance": 30000}, "incoming": {"preset": "guristas"},
             "constraints": {"min_ehp": 12000, "cap_stable": True},
             "fitting": {"tank": "shield", "propulsion": "ab", "meta": ["tech1", "tech2", "storyline"],
                         "require": [], "exclude": []},
             "search": {"effort": "quick"}},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--pyodide", required=True, help="path to the pyodide npm package")
    ap.add_argument("--data", help="static data file (default: evesim's cache)")
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
    first = next(iter(samples.values()))
    initial = json.loads(webapi.simulate(json.dumps({"eft": first, "skills": "all5", "profile": "uniform",
                                                     "target": {"preset": "cruiser", "distance": 20000}})))
    initial_opt = json.loads(webapi.optimize(json.dumps(DEFAULT_OPT), lambda m: None))

    html = (HERE / "index.html").read_text()
    html = html.replace(html[html.index("const SAMPLES = "):html.index(";\nconst INITIAL = ")],
                        "const SAMPLES = " + json.dumps(samples))
    html = html.replace("const INITIAL = null;", "const INITIAL = " + json.dumps(initial) + ";")
    html = html.replace("const INITIAL_OPT = null;", "const INITIAL_OPT = " + json.dumps(initial_opt) + ";")
    (out / "index.html").write_text(html)
    print(f"built {out}")


if __name__ == "__main__":
    main()
