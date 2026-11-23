/* Dashboard interaction checks using DOM emulation, without a browser or MT5.
 * Start: .venv\Scripts\python scripts/preview_dashboard.py --port 8766
 * Install jsdom outside the repository, then expose it through NODE_PATH:
 * npm install --prefix "$env:TEMP/tradebot-ui-dom-validation" jsdom@26
 * $env:NODE_PATH = "$env:TEMP/tradebot-ui-dom-validation/node_modules"
 * node tests/dashboard_dom.cjs
 * Optional first argument: the synthetic preview's localhost URL.
 * Optional TRADEBOT_PAGE=src/tradebot/ui/static/index.html: read the page from disk (antivirus).
 * These checks do not evaluate CSS layout or render canvas pixels.
 */
"use strict";
const assert = require("node:assert/strict");
const { JSDOM, VirtualConsole } = require("jsdom");

const base = new URL(process.argv[2] || "http://127.0.0.1:8766/");
assert(["127.0.0.1", "localhost"].includes(base.hostname), "Use a local synthetic preview");
let checks = 0;
const check = (condition, message) => { assert(condition, message); checks++; };
const waitFor = async (predicate) => {
  for (let i = 0; i < 100; i++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.fail("Dashboard did not finish rendering");
};

(async () => {
  const [html, snapshot, plan] = await Promise.all([
    // TRADEBOT_PAGE reads the page from disk: some antivirus web shields hold a changed page from
    // localhost for minutes, while the JSON endpoints answer at once.
    process.env.TRADEBOT_PAGE ? require("node:fs").promises.readFile(process.env.TRADEBOT_PAGE, "utf8")
      : fetch(base).then(response => response.text()),
    fetch(new URL("/api/state", base)).then(response => response.json()),
    fetch(new URL("/api/plan", base)).then(response => response.json()),
  ]);
  assert(snapshot.account.server.includes("PREVIEW"), "Synthetic preview data is required");
  const charts = Object.fromEntries(await Promise.all(snapshot.symbols.map(async row => [row.symbol,
    await fetch(new URL(`/api/chart?symbol=${encodeURIComponent(row.symbol)}`, base)).then(response => response.json())])));

  async function boot({ mobile = false, ready = true, state = snapshot } = {}) {
    const errors = [];
    const media = new Map();
    const virtualConsole = new VirtualConsole();
    virtualConsole.on("jsdomError", error => errors.push(error.message));
    const dom = new JSDOM(html, {
      url: base.href,
      runScripts: "dangerously",
      pretendToBeVisual: true,
      virtualConsole,
      beforeParse(window) {
        window.matchMedia = query => {
          if (!media.has(query)) {
            const listeners = [];
            media.set(query, {
              matches: query.includes("max-width: 760px") && mobile,
              media: query,
              addEventListener: (_type, listener) => listeners.push(listener),
              removeEventListener() {},
              change(matches) { this.matches = matches; listeners.forEach(listener => listener(this)); },
            });
          }
          return media.get(query);
        };
        window.HTMLElement.prototype.scrollIntoView = function () {};
        window.HTMLCanvasElement.prototype.getContext = () => ({});
        window.HTMLDialogElement.prototype.showModal = function () {
          this._previousFocus = window.document.activeElement;
          this.setAttribute("open", "");
          this.querySelector("input, button, select")?.focus();
        };
        window.HTMLDialogElement.prototype.close = function () {
          this.removeAttribute("open");
          this.dispatchEvent(new window.Event("close"));
          this._previousFocus?.focus();
        };
        window.setInterval = () => 0;
        window.setTimeout = () => 0;
        window.requestAnimationFrame = () => 0;
        window.fetch = async (input, options = {}) => {
          assert(!options.method || options.method === "GET", "DOM checks must not mutate preview state");
          const url = new URL(input, base);
          const value = url.pathname === "/api/state" ? (ready ? state : { ready: false, symbols: [] })
            : url.pathname === "/api/chart" ? charts[url.searchParams.get("symbol")]
              : url.pathname === "/api/plan" ? plan : undefined;
          return { ok: value !== undefined, status: value === undefined ? 404 : 200, json: async () => structuredClone(value) };
        };
      },
    });
    if (ready) await waitFor(() => dom.window.document.querySelectorAll("#watchlist .sym").length === state.symbols.length);
    else await new Promise(resolve => setTimeout(resolve, 10));
    return { dom, window: dom.window, document: dom.window.document, errors, media };
  }

  const desktop = await boot();
  const { window, document, errors } = desktop;
  const $ = selector => document.querySelector(selector);
  const click = selector => { check(Boolean($(selector)), `Control exists: ${selector}`); $(selector).click(); };
  const input = (selector, text) => { $(selector).value = text; $(selector).dispatchEvent(new window.Event("input", { bubbles: true })); };
  check(errors.length === 0, `Startup errors: ${errors.join("; ")}`);
  check(document.querySelectorAll(".kpi").length === 4, "Account overview renders all four metrics");
  check($("#instrument-select").options.length === snapshot.symbols.length, "Every instrument is discoverable from the global selector");
  const chart = $(".chart-card"), chartHome = chart.parentElement;
  for (const view of ["signal", "market", "assessment", "signals", "positions", "activity", "risk", "dashboard"]) {
    click(`.nav [data-view="${view}"]`);
    check(document.querySelectorAll(".content > .view:not([hidden])").length === 1, `Exactly one view is visible: ${view}`);
    check(!document.querySelector(`.content > [data-view="${view}"]`).hidden, `${view} is reachable`);
    check(document.querySelectorAll("#chart").length === 1, `${view} preserves the single chart`);
    if (view === "signal") check(chart.previousElementSibling.id === "advice", "Signal page receives the existing chart");
  }
  check(chart.parentElement === chartHome, "Dashboard restores the chart to its original container");

  input("#watch-search", "xau");
  check(document.querySelectorAll("#watchlist .sym").length === 1, "Watchlist search is case insensitive");
  click("#watchlist .sym");
  check($("#instrument-select").value === "XAUUSD", "Watchlist selection updates the global instrument");
  input("#watch-search", "NO_SUCH_SYMBOL");
  check($("#watchlist").textContent.includes("No matching"), "Watchlist has an explicit empty result");
  input("#watch-search", "");
  check(document.querySelectorAll("#watchlist .sym").length === snapshot.symbols.length, "Clearing filter restores every instrument");
  click('.nav [data-view="assessment"]');
  $("#instrument-select").value = "EURUSD";
  $("#instrument-select").dispatchEvent(new window.Event("change", { bubbles: true }));
  click('.nav [data-view="dashboard"]');
  check($("#chart-name").textContent === "EURUSD", "Instrument changes from another page carry through to the chart");
  check($("#watchlist [aria-current='true']").dataset.symbol === "EURUSD", "Selected watchlist row remains in sync");

  click("#search-open");
  check($("#command").open, "Search opens successfully");
  input("#command-input", "drawdown");
  check($("#command-results").textContent.includes("Risk limits"), "Search finds pages through their feature names");
  $("#command-input").dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  check(!$("#command").open && !document.querySelector('[data-view="risk"].view').hidden, "Enter opens the selected search result");
  document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true }));
  check($("#command").open, "Ctrl+K opens search");
  input("#command-input", "");
  const firstActive = $("#command-input").getAttribute("aria-activedescendant");
  $("#command-input").dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
  check($("#command-input").getAttribute("aria-activedescendant") !== firstActive, "Arrow keys update accessible active search result");
  input("#command-input", "NO_MATCH_ANYWHERE");
  check($("#command-results").textContent.includes("No matches"), "Search explains no matches");
  check(!$("#command-input").hasAttribute("aria-activedescendant"), "No-match search clears the active descendant");
  input("#command-input", "GBPUSD");
  click("#command-results button");
  check($("#instrument-select").value === "GBPUSD" && !document.querySelector('.view[data-view="dashboard"]').hidden, "Search opens a matching instrument on the dashboard");
  click("#search-open");
  input("#command-input", "Appearance");
  const previousTheme = document.documentElement.dataset.theme;
  click("#command-results button");
  check(document.documentElement.dataset.theme !== previousTheme, "Search can activate a tool");
  check(errors.length === 0, `Interaction errors: ${errors.join("; ")}`);
  desktop.dom.window.close();

  // Orders on: a bot signal its limits held back offers Get in, and the bot's trade offers Duplicate.
  // The confirm dialogs open with the server's figures; nothing is sent (Go is never pressed).
  const live = structuredClone(snapshot);
  live.gate = { execute: true, reason: "demo account", live_flag: true };
  live.book = live.book ? { ...live.book, active: null } : live.book;
  const first = live.symbols[0];
  const buy = first.advice.plans.buy;
  first.advice = { ...first.advice, action: "SKIP", side: 1, actionable: false, blocker: "daily loss limit: -40.00 (-3.6%)",
    plans: { ...first.advice.plans, buy: { ...buy, volume: 0, rejected: "" } }, plan: { ...buy, volume: 0 } };
  first.get_in = { side: 1, over: "daily loss limit: -40.00 (-3.6%)", plan: { ...buy, volume: 0.05, risk_amount: 15, reward_amount: 30 } };
  live.positions = [{ ticket: 77, symbol: first.symbol, side: 1, volume: 0.05, price_open: buy.entry, sl: buy.stop, tp: buy.target,
    time_open: live.now, initial_volume: 0.05, initial_sl: buy.stop, profit: 0, comment: "tradebot", magic: 230504, bot: true,
    r: 0, reversal: null, book: false,
    duplicate: { ticket: 77, symbol: first.symbol, side: 1, volume: 0.05, entry: buy.entry, stop: buy.stop, target: buy.target,
      sl_distance: buy.stop_distance, tp_distance: buy.target_distance, risk_amount: 15, reward_amount: 30, risk_percent: 1.4 } }];
  const orders = await boot({ state: live });
  const o = selector => orders.document.querySelector(selector);
  orders.document.querySelector('.nav [data-view="signal"]').click();
  const getIn = [...orders.document.querySelectorAll("#plan .order-btn")].find(b => /^Get in/.test(b.textContent));
  check(Boolean(getIn) && !getIn.disabled && getIn.classList.contains("over"), "A held-back bot signal offers Get in over the limit");
  getIn.click();
  check(o("#confirm").open && /^Get in: BUY 0.05 lots/.test(o("#confirm-title").textContent), "Get in asks to confirm the uncapped size");
  check(/daily loss limit/.test(o("#confirm-body").textContent) && /your own call/.test(o("#confirm-body").textContent), "Get in names the limit and says it is logged as manual");
  o("#confirm-cancel").click();
  orders.document.querySelector('.nav [data-view="positions"]').click();
  const dup = [...orders.document.querySelectorAll("#positions button")].find(b => b.textContent === "Duplicate");
  check(Boolean(dup) && !dup.disabled, "The bot's trade offers Duplicate while orders are on");
  dup.click();
  check(o("#confirm").open && /^Duplicate the bot's BUY 0.05/.test(o("#confirm-title").textContent), "Duplicate asks to confirm the copy");
  check(/#77/.test(o("#confirm-body").textContent), "Duplicate names the trade it copies");
  o("#confirm-cancel").click();
  live.positions[0].duplicate = { refused: "price is too close to the trade's stop to copy it" };
  const refused = await boot({ state: live });
  refused.document.querySelector('.nav [data-view="positions"]').click();
  const off = [...refused.document.querySelectorAll("#positions button")].find(b => b.textContent === "Duplicate");
  check(Boolean(off) && off.disabled && /too close/.test(off.title), "Duplicate says why it cannot copy a trade");
  check(orders.errors.length === 0 && refused.errors.length === 0, `Order button errors: ${[...orders.errors, ...refused.errors].join("; ")}`);
  orders.dom.window.close();
  refused.dom.window.close();

  // An instrument on its own setup in the config: "As configured" stands for that, and a setup
  // button reads as chosen only while every instrument shows it (the switch is not sent here).
  const mixed = structuredClone(snapshot);
  mixed.setups = { ...mixed.setups, [mixed.symbols[1].symbol]: "breakout" };
  mixed.configured_setups = { ...mixed.setups };
  mixed.as_configured = true;
  const setupsPage = await boot({ state: mixed });
  const setupButton = text => [...setupsPage.document.querySelectorAll("#tfs .setups button")].find(b => b.textContent === text);
  check(setupButton("As configured")?.getAttribute("aria-pressed") === "true", "The setup bar offers the config's own setups, pressed while shown");
  check(setupButton("EMA cross")?.getAttribute("aria-pressed") === "false", "A setup does not read as chosen while an instrument watches another");
  check(setupsPage.errors.length === 0, `Setup bar errors: ${setupsPage.errors.join("; ")}`);
  setupsPage.dom.window.close();

  const phone = await boot({ mobile: true });
  const p = selector => phone.document.querySelector(selector);
  check(p("#sidebar").inert === true, "Closed mobile navigation is not keyboard focusable");
  for (let round = 0; round < 3; round++) {
    p("#menu-toggle").click();
    check(phone.document.body.classList.contains("nav-open"), `Mobile menu opens after ${round} page changes`);
    check(p("#sidebar").inert === false && p("#menu-toggle").getAttribute("aria-expanded") === "true", "Open menu accessibility state matches visibility");
    p(`.nav [data-view="${round % 2 ? "dashboard" : "risk"}"]`).click();
    check(!phone.document.body.classList.contains("nav-open"), "Selecting a page closes mobile navigation");
  }
  p("#menu-toggle").click();
  phone.document.dispatchEvent(new phone.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  check(!phone.document.body.classList.contains("nav-open") && phone.document.activeElement === p("#menu-toggle"), "Escape dismisses navigation and restores focus");
  phone.media.get("(max-width: 760px)").change(false);
  check(p("#sidebar").inert === false, "Resizing to desktop restores sidebar access");
  check(phone.errors.length === 0, `Mobile errors: ${phone.errors.join("; ")}`);
  phone.dom.window.close();
  console.log(`${checks} dashboard DOM checks passed (synthetic data; no browser, MT5 or order requests).`);
})().catch(error => { console.error(error); process.exitCode = 1; });
