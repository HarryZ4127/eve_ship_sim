// Runs evesim under Pyodide off the main thread. Messages:
//   in:  {id, op: "init" | "simulate" | "simulate_fit" | "ship_info" | "charges" | "parse_eft" |
//              "optimize" | "train", payload}
//   out: {id, ok, result | error} and {type: "log", msg} / {type: "status", msg}
const base = new URL("./", self.location.href).href;
importScripts(base + "pyodide/pyodide.js");

let py = null;

// The host serves no .zip files, so the standard library arrives as base64 text and is
// handed to Pyodide through a fetch shim for the URL it asks for.
async function installStdlib() {
  const b64 = await (await fetch(base + "pyodide/python_stdlib.b64.txt")).text();
  const bin = atob(b64.trim());
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const realFetch = self.fetch.bind(self);
  self.fetch = (input, init) => {
    const url = String(input instanceof Request ? input.url : input);
    if (url.endsWith("python_stdlib.zip")) {
      return Promise.resolve(new Response(bytes, { headers: { "Content-Type": "application/zip" } }));
    }
    return realFetch(input, init);
  };
}

async function boot() {
  postMessage({ type: "status", msg: "Starting Python (first load is about 15 MB)" });
  await installStdlib();
  py = await loadPyodide({ indexURL: base + "pyodide/" });
  postMessage({ type: "status", msg: "Loading the simulator and game data" });
  const [src, data] = await Promise.all([
    fetch(base + "evesim_src.json").then((r) => r.json()),
    fetch(base + "staticdata.json").then((r) => r.arrayBuffer()),
  ]);
  for (const [path, text] of Object.entries(src)) {
    const full = "/lib/evesim/" + path;
    py.FS.mkdirTree(full.substring(0, full.lastIndexOf("/")));
    py.FS.writeFile(full, text);
  }
  py.FS.writeFile("/staticdata.json", new Uint8Array(data));
  py.runPython('import sys; sys.path.insert(0, "/lib/evesim"); import webapi');
  return py.runPython('webapi.init("/staticdata.json")');
}

const ready = boot();

self.onmessage = async (e) => {
  const { id, op, payload } = e.data;
  try {
    const info = await ready;
    let result;
    if (op === "init") result = info;
    else if (op === "catalog") result = py.runPython("webapi.catalog()");
    else if (["simulate", "simulate_fit", "ship_info", "charges", "parse_eft"].includes(op)) {
      py.globals.set("REQ", payload);
      result = py.runPython(`webapi.${op}(REQ)`);
    } else if (op === "optimize" || op === "train") {
      py.globals.set("REQ", payload);
      py.globals.set("LOG", (m) => postMessage({ type: "log", msg: m }));
      result = py.runPython(`webapi.${op}(REQ, LOG)`);
    } else throw new Error("unknown op " + op);
    postMessage({ id, ok: true, result });
  } catch (err) {
    let msg = String(err && err.message ? err.message : err);
    // Python tracebacks: keep the final "SomeError: message" line.
    const lines = msg.trim().split("\n");
    if (lines.length > 1) msg = lines[lines.length - 1];
    postMessage({ id, ok: false, error: msg });
  }
};
