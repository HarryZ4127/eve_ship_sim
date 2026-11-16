// Browser smoke test for a built demo (web/build.py output served over HTTP).
//
//   npm install playwright-core            (or playwright, which brings a browser)
//   python -m http.server -d dist 8000
//   node web/smoke_test.mjs http://localhost:8000/index.html
//
// Set CHROMIUM_PATH to a Chromium binary if playwright has none installed.
// Checks: Python boots, the fitting window simulates, the Real fits dialog loads a fit,
// a quick optimization finishes, no console errors, no sideways scrolling on a phone.
import { chromium } from "playwright-core";

const url = process.argv[2] || "http://localhost:8000/index.html";
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1360, height: 1000 } });
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
page.on("console", (m) => { if (m.type() === "error" && !m.text().includes("404")) errors.push(m.text()); });
const check = (ok, what) => { console.log(`${ok ? "ok  " : "FAIL"} ${what}`); if (!ok) process.exitCode = 1; };

await page.goto(url);
await page.waitForFunction(() => /ready|error/.test(document.querySelector("#status").className), null, { timeout: 180000 });
const status = await page.textContent("#status");
check(status.startsWith("Ready"), `Python ready (${status})`);
await page.waitForTimeout(1500);
check((await page.$$("#ring .slot")).length > 0, "fitting window shows slots");

await page.click("#fit-real");
// The list first shows a "Simulating ..." placeholder; wait for fits, an error or "no fits".
await page.waitForFunction(() => {
  const l = document.querySelector("#real-list");
  return l.querySelector(".card, .err") || (l.textContent.trim() && !l.textContent.includes("Simulating"));
}, null, { timeout: 120000 });
const cards = await page.$$("#real-list .card");
check(cards.length > 0, `Real fits dialog lists ${cards.length} fits`);
if (cards.length) {
  await page.click("#real-list [data-open] >> nth=0");
  await page.waitForTimeout(3000);
  check(Boolean(await page.$eval("#fit-summary .tile .v", (e) => e.textContent)), "a real fit loads and simulates");
} else {
  await page.click("#real-close");
}

await page.click("#tab-opt");
await page.fill("#opt-ship", "Rifter");
await page.selectOption("#f-tank", "any");
await page.click("#opt-go");
await page.waitForFunction(() => document.querySelector("#opt-go").textContent === "Find best fit", null, { timeout: 600000 });
const typ = await page.$$eval("#opt-out .card .sub", (els) => els.map((e) => e.textContent).filter((t) => t.startsWith("Typicality")));
check(typ.length > 0, `optimizer result shows typicality (${(typ[0] || "").trim().slice(0, 40)})`);

await page.setViewportSize({ width: 390, height: 900 });
check(!(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)), "no sideways scrolling at phone width");
check(errors.length === 0, `no console errors${errors.length ? ": " + errors.join(" | ") : ""}`);
await browser.close();
