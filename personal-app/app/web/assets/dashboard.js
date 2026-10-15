import { createDemo } from "./demo.mjs";
import { Orb } from "./orb.mjs";
import {
  shortAddress,
  count,
  humanize,
  timeUTC,
  age,
  sparkPoints,
  eventColor,
  finalityColor,
  finalityLabel,
  relationshipSeparator,
  validSearch,
} from "./format.mjs";

const $ = (id) => document.getElementById(id);
const node = (tag, className = "", text = "") => {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== null) result.textContent = text;
  return result;
};
const svgNode = (tag, attributes = {}) => {
  const result = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes))
    result.setAttribute(key, String(value));
  return result;
};
const action = (label, callback, className = "button") => {
  const result = node("button", className, label);
  result.type = "button";
  result.addEventListener("click", callback);
  return result;
};
const badge = (label, color = finalityColor(label)) =>
  node("span", `badge ${color}`, humanize(label).toUpperCase());
const demo = createDemo();
const requestedSource = new URL(location.href).searchParams.get("source");
let savedSource;
try {
  savedSource = localStorage.getItem("quantumorb.source");
} catch {}
const state = {
  source:
    requestedSource === "demo" || (!requestedSource && savedSource === "demo")
      ? "demo"
      : "local",
  route: "orb",
  snapshot: null,
  paused: false,
  loading: false,
  version: 0,
  detailVersion: 0,
  offsets: { tokens: 0, wallets: 0, activity: 0 },
  requests: {},
  pages: {},
  highlightAgent: null,
};
const PAGE_SIZE = 15;
const dialog = $("detail-dialog");
new Orb($("orb-canvas"), $("toggle-motion"));

function notify(text) {
  $("toast").textContent = text;
  $("toast").hidden = false;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => ($("toast").hidden = true), 3500);
}
async function copy(value) {
  try {
    await navigator.clipboard.writeText(value);
    notify("Public address copied.");
  } catch {
    notify(
      "Clipboard unavailable. You can select the full address in token details.",
    );
  }
}
function showDialog(title) {
  $("detail-heading").textContent = title;
  $("detail-source").textContent =
    state.source === "demo" ? "· SAMPLE DATA" : "· LOCAL INDEX";
  if (!dialog.open) dialog.showModal();
}
document
  .querySelector(".close-dialog")
  .addEventListener("click", () => dialog.close());
dialog.addEventListener("close", () => {
  state.detailVersion += 1;
});
dialog.addEventListener("click", (event) => {
  if (event.target === dialog) {
    const box = dialog.getBoundingClientRect();
    if (
      event.clientX < box.left ||
      event.clientX > box.right ||
      event.clientY < box.top ||
      event.clientY > box.bottom
    )
      dialog.close();
  }
});

async function api(path, signal) {
  const response = await fetch(path, {
    signal: signal || AbortSignal.timeout(10000),
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    const error = new Error(
      response.status === 422
        ? "Use a valid public address or signature prefix."
        : response.status === 404
          ? "This evidence has not been recorded."
          : "Local storage is unavailable. Check System health.",
    );
    error.status = response.status;
    throw error;
  }
  return response.json();
}
async function read(kind, options = {}, signal) {
  if (state.source === "demo") return demo[kind](options);
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(options))
    if (value !== "" && value !== null && value !== undefined)
      query.set(key, String(value));
  return api(`/api/${kind}?${query}`, signal);
}
const tokenName = (mint) =>
  !mint
    ? "NOT READ"
    : state.source === "demo"
      ? demo.tokens({ q: mint, limit: 1 }).items[0]?.symbol ||
        shortAddress(mint)
      : shortAddress(mint);
function addressLink(value, kind = "token", compact = false) {
  if (!value) return node("span", "muted", "Not read");
  const result = action(
    "",
    () => (kind === "token" ? openToken(value) : filterWallet(value)),
    "address-button",
  );
  result.title = value;
  if (kind === "token" && !compact) {
    result.append(node("span", "token-avatar", tokenName(value).slice(0, 2)));
    const identity = node("span", "token-identity");
    identity.append(
      node("span", "", tokenName(value)),
      node(
        "small",
        "",
        state.source === "demo" ? shortAddress(value) : "PUBLIC MINT",
      ),
    );
    result.append(identity);
  } else
    result.textContent =
      kind === "token" ? tokenName(value) : shortAddress(value);
  return result;
}
function eventLabel(event) {
  return node(
    "span",
    `event-label ${eventColor(event.event_type)}`,
    humanize(event.event_type).toUpperCase(),
  );
}
function cell(content, className = "") {
  const td = node("td", className);
  td.append(
    content instanceof Node
      ? content
      : document.createTextNode(String(content ?? "—")),
  );
  return td;
}
function rowAction(label, callback) {
  const button = action("↗", callback, "row-action");
  button.setAttribute("aria-label", label);
  button.title = label;
  return button;
}
function emptyRows(target, columns, title, description, offerDemo = false) {
  const row = node("tr"),
    td = node("td", "empty-state");
  td.colSpan = columns;
  td.append(node("strong", "", title), node("p", "", description));
  if (offerDemo)
    td.append(action("EXPLORE SAMPLE DATA", () => setSource("demo")));
  row.append(td);
  target.replaceChildren(row);
}
function renderActivityRows(target, items, preview = false) {
  if (!items.length) {
    emptyRows(
      target,
      preview ? 6 : 7,
      "No observations in this view.",
      "Try another observation window or search. Live intake runs through explicit backend commands.",
      state.source === "local",
    );
    return;
  }
  target.replaceChildren(
    ...items.map((event) => {
      const row = node("tr");
      row.append(
        cell(eventLabel(event)),
        cell(addressLink(event.mint, "token", preview)),
      );
      if (!preview) row.append(cell(addressLink(event.wallet, "wallet", true)));
      row.append(
        cell(count(event.slot), "muted"),
        cell(badge(event.finality)),
        cell(age(event.observed_at), "muted"),
        cell(rowAction("Inspect observation", () => openEvent(event))),
      );
      return row;
    }),
  );
}
function setNotice(kind, text, buttonText, callback) {
  const notice = $("data-notice");
  notice.dataset.kind = kind;
  notice.hidden = false;
  notice.replaceChildren(node("span", "", text));
  if (buttonText) notice.append(action(buttonText, callback));
}
function renderSnapshot(snapshot) {
  state.snapshot = snapshot;
  const c = snapshot.counters,
    health = snapshot.health;
  $("desk-events").textContent = count(c.events);
  $("desk-finalized").textContent = count(c.finalized);
  $("desk-last").textContent = snapshot.last_observation_at
    ? age(snapshot.last_observation_at).toUpperCase()
    : "NOT READ";
  $("desk-slot").textContent = snapshot.latest_slot
    ? `slot ${count(snapshot.latest_slot)}`
    : "slot unavailable";
  $("orb-node-count").textContent = `${count(c.nodes)} INDEXED NODES`;
  $("orb-source-label").textContent =
    state.source === "demo" ? "SAMPLE DATA" : "LOCAL INDEX";
  $("orb-update").textContent = `SNAPSHOT ${timeUTC(snapshot.observed_at)}`;
  $("header-status").textContent = health.ready
    ? "INDEX READY ↗"
    : "SETUP NEEDED ↗";
  $("header-status").classList.toggle("orange", !health.ready);
  $("ticker-label").replaceChildren(
    node("span", "status-dot"),
    document.createTextNode(
      state.source === "demo" ? " SAMPLE FEED" : " INDEXED FEED",
    ),
  );
  $("ticker-content").replaceChildren();
  for (const event of snapshot.recent.slice(0, 5)) {
    const label = node("span", "", humanize(event.event_type).toUpperCase());
    $("ticker-content").append(
      label,
      document.createTextNode(
        `${tokenName(event.mint)} · SLOT ${count(event.slot)}   /   `,
      ),
    );
  }
  if (!snapshot.recent.length)
    $("ticker-content").textContent =
      "No indexed observations yet. Connect the local database or explore sample data.";
  $("footer-source").textContent =
    state.source === "demo"
      ? "SAMPLE DATA · READ-ONLY PREVIEW"
      : "LOCAL INDEX · READ-ONLY RESEARCH";
  if (state.source === "demo")
    setNotice(
      "demo",
      "SAMPLE WORKSPACE — All displayed activity is illustrative. No live connection or database writes.",
      "USE LOCAL DATA",
      () => setSource("local"),
    );
  else if (!health.ready)
    setNotice(
      "error",
      "Local database is not ready. Run the documented init-db command, or explore the sample workspace.",
      "TRY SAMPLE DATA",
      () => setSource("demo"),
    );
  else if (snapshot.warnings.includes("observations_stale"))
    setNotice(
      "warning",
      `The latest indexed observation is ${age(snapshot.last_observation_at)}. Provider connection is unverified.`,
      "SYSTEM HEALTH",
      () => {
        location.hash = "system";
      },
    );
  else if (!snapshot.recent.length)
    setNotice(
      "warning",
      "The database is ready. No canonical observations have been recorded yet.",
      "TRY SAMPLE DATA",
      () => setSource("demo"),
    );
  else $("data-notice").hidden = true;
  renderObservables(snapshot);
  renderStack(snapshot.recent);
  renderAgents(snapshot.agents);
  renderCounters(c);
  renderActivityRows($("preview-rows"), snapshot.recent.slice(0, 5), true);
  renderSystem(snapshot);
}
function renderObservables(snapshot) {
  const c = snapshot.counters;
  const observables = [
    ["LAUNCHES / 24H", count(c.launches_24h), "RECORDED CREATES", "blue", null],
    [
      "EVENTS READ",
      count(c.events),
      "HOURLY OBSERVATIONS",
      "yellow",
      snapshot.series.map((item) => item.events),
    ],
    [
      "ACTIVE WALLETS",
      count(c.wallets_1h),
      "EXPLICIT USERS / 1H",
      "green",
      null,
    ],
    ["BUNDLE EVIDENCE", "NOT READ", "NO VERIFIED BUNDLE FEED", "red", null],
    ["RISK MODEL", "UNAVAILABLE", "SCREENING NOT RUNNING", "orange", null],
    [
      "POOLS / 24H",
      count(c.pools_24h),
      "RECORDED POOL CREATES",
      "purple",
      null,
    ],
    ["FINALIZED", count(c.finalized), "RECONCILED EVENTS", "blue", null],
  ];
  $("observables").replaceChildren(
    ...observables.map(([label, value, description, color, series]) => {
      const section = node("div", `observable ${color}`);
      section.dataset.unavailable = String(
        value === "NOT READ" || value === "UNAVAILABLE",
      );
      const top = node("div", "observable-top");
      top.append(node("span", "", label), node("strong", "", value));
      section.append(top, node("small", "", description));
      const svg = svgNode("svg", {
        viewBox: "0 0 90 15",
        "aria-hidden": "true",
      });
      if (series)
        svg.append(
          svgNode("polyline", { points: sparkPoints(series, 90, 13) }),
        );
      else
        svg.append(
          svgNode("polyline", {
            points: "0,10 90,10",
            "stroke-dasharray":
              value === "NOT READ" || value === "UNAVAILABLE" ? "2 3" : "0",
            opacity: ".45",
          }),
        );
      section.append(svg);
      return section;
    }),
  );
}
function renderStack(events) {
  if (!events.length) {
    $("evidence-stack").replaceChildren(
      node(
        "p",
        "empty-inline",
        "No observations indexed. The evidence stack fills as Scout records events.",
      ),
    );
    return;
  }
  $("evidence-stack").replaceChildren(
    ...events.slice(0, 9).map((event) => {
      const button = action(
        "",
        () => (event.mint ? openToken(event.mint) : openEvent(event)),
        "stack-row",
      );
      const info = node("span");
      info.append(
        node(
          "strong",
          "",
          `${tokenName(event.mint)} / ${humanize(event.event_type)}`,
        ),
        node(
          "small",
          "",
          `${timeUTC(event.observed_at)} · ${finalityLabel(event.finality)}`,
        ),
      );
      button.append(
        node(
          "span",
          `stack-symbol ${eventColor(event.event_type)}`,
          event.event_type.includes("sell")
            ? "↘"
            : event.event_type === "token_created"
              ? "✦"
              : "↗",
        ),
        info,
      );
      return button;
    }),
  );
}
function agentIcon(id) {
  const svg = svgNode("svg", {
    class: "agent-icon",
    "aria-hidden": "true",
    viewBox: "0 0 48 48",
  });
  svg.append(svgNode("use", { href: `#icon-${id}` }));
  return svg;
}
function renderAgents(agents) {
  $("agent-strip").replaceChildren(
    ...agents.map((agent) => {
      const button = action(
        "",
        () => {
          state.highlightAgent = agent.id;
          location.hash = "agents";
          highlightAgent();
        },
        `agent-tile ${agent.color}`,
      );
      button.setAttribute(
        "aria-label",
        `${agent.name}: ${humanize(agent.state)}. Inspect agent.`,
      );
      button.append(
        agentIcon(agent.id),
        node("strong", "", agent.code),
        node("span", "agent-name", agent.name.toUpperCase()),
        node("span", "agent-metric", count(agent.metric)),
        node(
          "span",
          "agent-unit",
          agent.metric === null ? "NOT AVAILABLE" : agent.unit.toUpperCase(),
        ),
      );
      return button;
    }),
  );
  $("agents-grid").replaceChildren(
    ...agents.map((agent) => {
      const card = node("article", `agent-card ${agent.color}`);
      card.id = `agent-${agent.id}`;
      const heading = node("div", "agent-card-heading"),
        title = node("div");
      title.append(
        badge(agent.code, agent.color),
        node("h2", "", agent.name.toUpperCase()),
      );
      heading.append(
        agentIcon(agent.id),
        title,
        node(
          "span",
          "agent-state",
          agent.state === "not_implemented"
            ? "PLANNED"
            : humanize(agent.state).toUpperCase(),
        ),
      );
      const metric = node("div", "agent-card-metric");
      metric.append(
        node("strong", "", count(agent.metric)),
        node("small", "", agent.unit),
      );
      card.append(
        heading,
        node("p", "", agent.description),
        metric,
        node("p", "agent-flow", `IN → ${agent.input}`),
        node("p", "agent-flow", `OUT → ${agent.output}`),
      );
      return card;
    }),
  );
  highlightAgent();
}
function highlightAgent() {
  document
    .querySelectorAll(".agent-card")
    .forEach((card) =>
      card.classList.toggle(
        "highlighted",
        card.id === `agent-${state.highlightAgent}`,
      ),
    );
}
function renderCounters(c) {
  const counters = [
    ["LAUNCHES · 24H", c.launches_24h, "indexed token creates", ""],
    ["POOLS · 24H", c.pools_24h, "indexed pool creates", "yellow"],
    ["ACTIVE WALLETS · 1H", c.wallets_1h, "explicit observed users", ""],
    ["RELATIONSHIPS", c.relationships, "stored evidence edges", "purple"],
    ["RAW OBSERVATIONS", c.raw_observations, "retained in the journal", "blue"],
    ["ANALYSES", c.analyses, "completed / partial runs", "green"],
  ];
  $("counter-strip").replaceChildren(
    ...counters.map(([label, value, note, color]) => {
      const card = node("article", "counter");
      card.append(
        node("span", "", label),
        node("strong", color, count(value)),
        node("small", "", note),
      );
      return card;
    }),
  );
}
function renderSystem(snapshot) {
  const health = snapshot.health;
  const facts = [
    [
      "Data source",
      state.source === "demo" ? "Sample workspace" : "Local index",
    ],
    ["Database", health.database_state],
    ["Configured mode", health.mode],
    ["Configured transport", health.transport],
    ["Provider connection", health.provider_state],
    ["Ingestion process", health.ingestion_state],
    [
      "Last observation",
      snapshot.last_observation_at
        ? `${timeUTC(snapshot.last_observation_at)} / ${age(snapshot.last_observation_at)}`
        : "Not read",
    ],
    ["Last job status", health.last_run_status || "Not read"],
  ];
  $("system-facts").replaceChildren(
    ...facts.map(([key, value]) => {
      const div = node("div");
      div.append(node("dt", "", key), node("dd", "", humanize(value)));
      return div;
    }),
  );
  const max = Math.max(1, ...snapshot.series.map((item) => item.events));
  $("history-bars").replaceChildren(
    ...snapshot.series.map((item) => {
      const bar = node("div", "history-bar");
      const svg = svgNode("svg", {
        viewBox: "0 0 10 150",
        preserveAspectRatio: "none",
        role: "img",
        "aria-label": `${item.hour}: ${item.events} events`,
      });
      const title = svgNode("title");
      title.textContent = `${item.hour}: ${item.events} events`;
      svg.append(
        title,
        svgNode("rect", {
          x: 0,
          y: 149 - (item.events / max) * 140,
          width: 10,
          height: Math.max(1, (item.events / max) * 140),
        }),
      );
      bar.append(svg);
      return bar;
    }),
  );
  const available = [
    [
      "blue",
      "Canonical observations",
      `${count(snapshot.counters.events)} local events. Counts describe retained observations, not the entire network.`,
    ],
    [
      "purple",
      "Bounded wallet history",
      "Coverage is recorded per analysis. Missing wallet history stays unknown.",
    ],
    [
      "yellow",
      "Provider authentication",
      "Not verified by this dashboard. Reading local state never starts a provider connection.",
    ],
    [
      "orange",
      "Research models",
      "Judge, Quant and Guard are planned. No probability estimates or risk scores are emitted.",
    ],
    [
      "red",
      "Execution disabled",
      "No transaction submission, signing or trading is available.",
    ],
  ];
  $("availability").replaceChildren(
    ...available.map(([color, title, description]) => {
      const item = node("div", `availability-item ${color}`),
        text = node("div");
      text.append(node("strong", "", title), node("small", "", description));
      item.append(node("span"), text);
      return item;
    }),
  );
}
function optionsFor(kind) {
  if (kind === "tokens")
    return {
      q: $("token-search").value.trim(),
      lifecycle: $("lifecycle-filter").value,
    };
  if (kind === "wallets")
    return {
      q: $("wallet-search").value.trim(),
      window: $("wallet-window").value,
    };
  return {
    q: $("activity-search").value.trim(),
    window: $("activity-window").value,
    finality: $("finality-filter").value,
  };
}
function filterMessage(kind) {
  return $(
    kind === "tokens"
      ? "token-filter-message"
      : kind === "wallets"
        ? "wallet-filter-message"
        : "activity-filter-message",
  );
}
async function loadPage(kind) {
  const options = optionsFor(kind),
    message = filterMessage(kind),
    target = $(
      kind === "tokens"
        ? "token-rows"
        : kind === "wallets"
          ? "wallet-rows"
          : "activity-rows",
    );
  const columns = kind === "wallets" ? 7 : 7;
  message.textContent = "";
  if (!validSearch(options.q)) {
    message.textContent =
      "Search with a public address or signature prefix (Base58 characters).";
    state.requests[kind]?.abort();
    return;
  }
  state.requests[kind]?.abort();
  const controller = new AbortController();
  state.requests[kind] = controller;
  const version = state.version;
  if (!state.pages[kind])
    emptyRows(target, columns, "Reading the local index…", "");
  try {
    const result = await read(
      kind,
      { ...options, limit: PAGE_SIZE, offset: state.offsets[kind] },
      controller.signal,
    );
    if (controller.signal.aborted || version !== state.version) return;
    state.pages[kind] = result;
    if (kind === "tokens") renderTokens(result);
    else if (kind === "wallets") renderWallets(result);
    else renderActivityRows(target, result.items);
    $(`${kind}-total`).textContent =
      `${count(result.total)} ${kind === "wallets" ? "WALLETS" : kind === "tokens" ? "TOKENS" : "EVENTS"}`;
    renderPagination(kind, result);
  } catch (error) {
    if (error.name === "AbortError") return;
    emptyRows(
      target,
      columns,
      "The index is unavailable.",
      error.message,
      state.source === "local",
    );
    $(`${kind}-pagination`).replaceChildren();
    $(`${kind}-total`).textContent = "UNAVAILABLE";
  }
}
function renderTokens(page) {
  if (!page.items.length) {
    emptyRows(
      $("token-rows"),
      7,
      "No tokens match this view.",
      "Clear the address search or choose another lifecycle.",
      state.source === "local",
    );
    return;
  }
  $("token-rows").replaceChildren(
    ...page.items.map((token) => {
      const row = node("tr");
      row.append(
        cell(addressLink(token.mint)),
        cell(
          badge(
            token.lifecycle,
            token.lifecycle.startsWith("pool") ? "purple" : "blue",
          ),
        ),
        cell(addressLink(token.creator, "wallet", true)),
        cell(count(token.event_count)),
        cell(
          badge(
            token.provisional === null
              ? "not_read"
              : token.provisional
                ? "provisional"
                : "finalized",
          ),
        ),
        cell(age(token.last_seen_at), "muted"),
        cell(rowAction("Inspect token evidence", () => openToken(token.mint))),
      );
      return row;
    }),
  );
}
function renderWallets(page) {
  if (!page.items.length) {
    emptyRows(
      $("wallet-rows"),
      7,
      "No wallets match this view.",
      "Try another public address or observation window.",
      state.source === "local",
    );
    return;
  }
  $("wallet-rows").replaceChildren(
    ...page.items.map((wallet) => {
      const row = node("tr");
      row.append(
        cell(addressLink(wallet.wallet, "wallet", true)),
        cell(count(wallet.event_count)),
        cell(count(wallet.trade_count)),
        cell(count(wallet.token_count)),
        cell(count(wallet.finalized_count), "green"),
        cell(age(wallet.last_seen_at), "muted"),
        cell(
          rowAction("Filter activity for this wallet", () =>
            filterWallet(wallet.wallet),
          ),
        ),
      );
      return row;
    }),
  );
}
function renderPagination(kind, page) {
  const target = $(`${kind}-pagination`),
    buttons = node("div", "page-buttons");
  const previous = action("← PREVIOUS", () => {
    state.offsets[kind] = Math.max(0, page.offset - page.limit);
    loadPage(kind);
  });
  previous.disabled = page.offset === 0;
  const next = action("NEXT →", () => {
    state.offsets[kind] = page.offset + page.limit;
    loadPage(kind);
  });
  next.disabled = !page.has_more;
  buttons.append(previous, next);
  target.replaceChildren(
    node(
      "span",
      "",
      page.total
        ? `${page.offset + 1}–${page.offset + page.items.length} OF ${count(page.total)} · LOCAL OBSERVATION ORDER`
        : "0 RESULTS",
    ),
    buttons,
  );
}
function filterWallet(wallet) {
  $("activity-search").value = wallet;
  state.offsets.activity = 0;
  location.hash = "activity";
  if (state.route === "activity") loadPage("activity");
}
function navigate() {
  const route = location.hash.slice(1).split("?")[0] || "orb";
  state.route = [
    "orb",
    "tokens",
    "traders",
    "activity",
    "agents",
    "system",
    "guide",
  ].includes(route)
    ? route
    : "orb";
  document.querySelectorAll(".page").forEach((page) => {
    page.hidden = page.id !== `page-${state.route}`;
  });
  document.querySelectorAll("[data-route]").forEach((link) => {
    if (link.dataset.route === state.route)
      link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  $("main-nav").classList.remove("open");
  $("menu-toggle").setAttribute("aria-expanded", "false");
  if (state.route === "tokens") loadPage("tokens");
  else if (state.route === "traders") loadPage("wallets");
  else if (state.route === "activity") loadPage("activity");
  if (state.route === "agents" && state.highlightAgent) {
    highlightAgent();
    requestAnimationFrame(() =>
      $(`agent-${state.highlightAgent}`)?.scrollIntoView({
        block: "nearest",
        behavior: "smooth",
      }),
    );
  }
}
function factList(entries) {
  const list = node("dl", "system-facts");
  for (const [key, value] of entries) {
    const row = node("div");
    row.append(node("dt", "", key));
    const dd = node("dd");
    dd.append(
      value instanceof Node
        ? value
        : document.createTextNode(String(value ?? "Not read")),
    );
    row.append(dd);
    list.append(row);
  }
  return list;
}
function openEvent(event) {
  state.detailVersion += 1;
  showDialog("Observation record");
  const content = $("detail-content");
  const signature = node("div", "detail-address");
  signature.append(
    node("code", "", event.signature),
    action("COPY", () => copy(event.signature)),
  );
  content.replaceChildren(
    signature,
    factList([
      ["Event", humanize(event.event_type)],
      ["Source program", humanize(event.program)],
      ["Slot", count(event.slot)],
      ["Finality", badge(event.finality)],
      ["Token", addressLink(event.mint, "token", true)],
      ["Wallet", event.wallet ? shortAddress(event.wallet) : "Not read"],
      ["Creator", event.creator ? shortAddress(event.creator) : "Not read"],
      ["Observed at", new Date(event.observed_at).toISOString()],
      [
        "Block time",
        event.block_time
          ? new Date(event.block_time).toISOString()
          : "Not read",
      ],
    ]),
  );
  content.append(
    node(
      "p",
      "panel-note",
      "Public canonical fields only. Raw payloads and private metadata are not exposed.",
    ),
  );
}
async function openToken(mint) {
  const version = ++state.detailVersion,
    source = state.source;
  showDialog(`${tokenName(mint)} / Evidence`);
  $("detail-content").replaceChildren(
    node("div", "empty-state", "Reading token evidence…"),
  );
  const getSummary = async () => {
    if (source === "demo") return demo.summary(mint);
    try {
      return await api(`/cartographer/tokens/${encodeURIComponent(mint)}`);
    } catch (error) {
      if (error.status === 404) return null;
      throw error;
    }
  };
  const getGraph = () =>
    source === "demo"
      ? demo.graph(mint, 80)
      : api(`/graph/export?mint=${encodeURIComponent(mint)}&limit=80`);
  const results = await Promise.allSettled([
    read("tokens", { q: mint, limit: 1 }),
    getSummary(),
    getGraph(),
    read("activity", { q: mint, limit: 10 }),
  ]);
  if (
    version !== state.detailVersion ||
    source !== state.source ||
    !dialog.open
  )
    return;
  const [tokenResult, summaryResult, graphResult, eventsResult] = results;
  if (tokenResult.status === "rejected" && eventsResult.status === "rejected") {
    $("detail-content").replaceChildren(
      node(
        "div",
        "empty-state",
        "Token storage is unavailable. Check System health and try again.",
      ),
    );
    return;
  }
  const token =
    tokenResult.status === "fulfilled"
      ? tokenResult.value.items.find((item) => item.mint === mint)
      : null;
  const summary =
      summaryResult.status === "fulfilled" ? summaryResult.value : null,
    graph = graphResult.status === "fulfilled" ? graphResult.value : null;
  const content = $("detail-content"),
    address = node("div", "detail-address");
  address.append(
    node("code", "", mint),
    action("COPY", () => copy(mint)),
  );
  const stats = node("div", "detail-stats");
  for (const [label, value, color] of [
    ["EVENTS", token?.event_count ?? null, "blue"],
    ["OBSERVED TRADERS", summary?.trader_count_observed ?? null, "green"],
    ["FUNDED WALLETS", summary?.wallets_with_inbound_funding ?? null, "purple"],
    ["SAME BUNDLE", summary?.same_bundle_groups ?? null, "yellow"],
  ]) {
    const card = node("div", "detail-stat");
    card.append(node("span", "", label), node("strong", color, count(value)));
    stats.append(card);
  }
  const tabs = node("div", "detail-tabs"),
    sections = node("div");
  const overview = node("section", "detail-section");
  overview.id = "detail-overview";
  overview.append(
    factList([
      ["Lifecycle", token ? badge(token.lifecycle, "blue") : "Not read"],
      [
        "Creator",
        token?.creator
          ? action(
              shortAddress(token.creator),
              () => copy(token.creator),
              "text-button",
            )
          : "Not read",
      ],
      [
        "Finality",
        token
          ? badge(
              token.provisional === null
                ? "not_read"
                : token.provisional
                  ? "provisional"
                  : "finalized",
            )
          : "Not read",
      ],
      [
        "Observation gap",
        token?.data_gap === null || token?.data_gap === undefined
          ? "Not read"
          : token.data_gap
            ? "Gap recorded"
            : "No gap recorded",
      ],
      [
        "Pool",
        token?.pool
          ? action(
              shortAddress(token.pool),
              () => copy(token.pool),
              "text-button",
            )
          : "Not read",
      ],
      ["Latest observation", token ? age(token.last_seen_at) : "Not read"],
    ]),
  );
  if (eventsResult.status === "fulfilled") {
    overview.append(node("h3", "", "RECENT OBSERVATIONS"));
    const wrapper = node("div", "table-scroll"),
      table = node("table"),
      tbody = node("tbody");
    renderActivityRows(tbody, eventsResult.value.items.slice(0, 5), true);
    table.append(tbody);
    wrapper.append(table);
    overview.append(wrapper);
  }
  const evidence = node("section", "detail-section");
  evidence.id = "detail-evidence";
  evidence.hidden = true;
  if (summary) {
    evidence.append(
      factList([
        ["Analysis state", badge(summary.state)],
        [
          "Coverage",
          summary.coverage.history_complete
            ? "Complete within recorded bounds"
            : "Partial / missing history",
        ],
        ["Wallets scanned", count(summary.coverage.wallets_scanned)],
        ["Transactions scanned", count(summary.coverage.transactions_scanned)],
        [
          "Missing wallet histories",
          count(summary.coverage.missing_wallet_histories),
        ],
        ["Shared funder groups", count(summary.shared_funder_groups)],
        ["Same transaction groups", count(summary.same_transaction_groups)],
        ["Same slot groups", count(summary.same_slot_groups)],
        [
          "Bundle evidence",
          summary.same_bundle_groups === null
            ? "Not read"
            : count(summary.same_bundle_groups),
        ],
        ["Algorithm", summary.algorithm_version],
        ["Observed at", age(summary.observed_at)],
      ]),
    );
    const warnings = node("div", "warning-list");
    for (const warning of summary.warnings || [])
      warnings.append(badge(warning, "orange"));
    evidence.append(
      warnings,
      node(
        "p",
        "",
        "Coverage refers to the recorded bounded analysis. It does not establish complete lifetime wallet history. Same-slot activity remains weak evidence.",
      ),
    );
  } else
    evidence.append(
      node(
        "h3",
        "",
        summaryResult.status === "rejected"
          ? "Analysis storage unavailable"
          : "No Cartographer analysis recorded.",
      ),
      node(
        "p",
        "",
        summaryResult.status === "rejected"
          ? "The saved analysis could not be read. Retry after checking System health."
          : "Funding, linked groups and wallet-history coverage are not read. A missing analysis does not establish zero funding or low risk.",
      ),
    );
  const relationship = node("section", "detail-section");
  relationship.id = "detail-relationships";
  relationship.hidden = true;
  if (graph) renderGraph(relationship, graph);
  else
    relationship.append(
      node(
        "p",
        "",
        "Graph storage is unavailable. Retry after checking System health.",
      ),
    );
  for (const [label, section] of [
    ["OVERVIEW", overview],
    ["EVIDENCE", evidence],
    ["RELATIONSHIPS", relationship],
  ]) {
    const button = action(
      label,
      () => {
        for (const child of sections.children) child.hidden = child !== section;
        for (const tab of tabs.children)
          tab.setAttribute("aria-pressed", String(tab === button));
      },
      "",
    );
    button.setAttribute("aria-pressed", String(section === overview));
    tabs.append(button);
    sections.append(section);
  }
  const actions = node("div", "detail-actions");
  actions.append(
    node(
      "span",
      "micro muted",
      source === "demo"
        ? "SAMPLE DATA / READ-ONLY"
        : "PUBLIC EVIDENCE / READ-ONLY",
    ),
  );
  const download = action("↓ EXPORT TOKEN GRAPH", () =>
    downloadJSON(
      graph,
      `quantumorb-${source === "demo" ? "sample-" : ""}${mint.slice(0, 8)}-graph.json`,
    ),
  );
  download.disabled = !graph;
  actions.append(download);
  content.replaceChildren(address, stats, tabs, sections, actions);
}
function renderGraph(target, graph) {
  if (!graph.edges.length) {
    target.append(
      node("h3", "", "No stored relationships for this token."),
      node(
        "p",
        "",
        "A Cartographer analysis must record evidence before it appears here. No connections are inferred from an empty graph.",
      ),
    );
    return;
  }
  target.append(
    node(
      "h3",
      "",
      `${count(graph.nodes.length)} NODES / ${count(graph.edges.length)} RELATIONSHIPS${graph.truncated ? " / BOUNDED VIEW" : ""}`,
    ),
  );
  const svg = svgNode("svg", {
      class: "graph-visual",
      viewBox: "0 0 700 320",
      role: "img",
      "aria-label":
        "Indexed token relationship graph. Nodes can be selected to copy public identifiers. A table lists each relationship below.",
    }),
    positions = new Map();
  const center =
      graph.nodes.find(
        (item) =>
          item.node_type === "token_mint" &&
          item.address_or_identifier === graph.mint,
      ) || graph.nodes[0],
    others = graph.nodes.filter((item) => item !== center);
  positions.set(center.id, { x: 350, y: 155 });
  others.forEach((item, i) => {
    const angle = (i * Math.PI * 2) / Math.max(1, others.length) - Math.PI / 2;
    positions.set(item.id, {
      x: 350 + Math.cos(angle) * 265,
      y: 155 + Math.sin(angle) * 115,
    });
  });
  for (const edge of graph.edges) {
    const from = positions.get(edge.source_node_id),
      to = positions.get(edge.target_node_id);
    if (!from || !to) continue;
    const line = svgNode("line", {
      class: "graph-line",
      x1: from.x,
      y1: from.y,
      x2: to.x,
      y2: to.y,
      "stroke-dasharray": edge.strength === "weak" ? "3 4" : "none",
    });
    const title = svgNode("title");
    title.textContent = `${humanize(edge.edge_type)} / ${edge.strength} / ${edge.evidence_state}`;
    line.append(title);
    svg.append(line);
  }
  for (const item of graph.nodes) {
    const point = positions.get(item.id);
    if (!point) continue;
    const link = svgNode("a", {
      href: "#",
      tabindex: 0,
      "aria-label": `Copy ${humanize(item.node_type)} ${item.address_or_identifier}`,
    });
    link.addEventListener("click", (event) => {
      event.preventDefault();
      copy(item.address_or_identifier);
    });
    const title = svgNode("title");
    title.textContent = `${humanize(item.node_type)}: ${item.address_or_identifier}`;
    const circle = svgNode("circle", {
      class: `graph-node ${item.node_type}`,
      cx: point.x,
      cy: point.y,
      r: item === center ? 13 : 7,
    });
    const label = svgNode("text", {
      x: point.x,
      y: point.y + (item === center ? 28 : 20),
      "text-anchor": "middle",
    });
    label.textContent = shortAddress(item.address_or_identifier, 4, 3);
    link.append(title, circle, label);
    svg.append(link);
  }
  target.append(
    svg,
    node(
      "p",
      "",
      `Stored relationships. Dashed lines indicate weak evidence. ${graph.truncated ? "This view reached its export bound. " : ""}Select a node to copy its public identifier.`,
    ),
  );
  const table = node("table", "graph-edges"),
    head = node("thead"),
    heading = node("tr");
  for (const label of ["RELATIONSHIP", "ENDPOINTS", "STRENGTH", "STATE"])
    heading.append(node("th", "", label));
  head.append(heading);
  const body = node("tbody");
  for (const edge of graph.edges) {
    const from = graph.nodes.find((item) => item.id === edge.source_node_id),
      to = graph.nodes.find((item) => item.id === edge.target_node_id),
      row = node("tr");
    const evidenceState = node("span", "edge-states");
    evidenceState.append(badge(edge.evidence_state));
    if (edge.status !== "active") evidenceState.append(badge(edge.status));
    row.append(
      cell(humanize(edge.edge_type)),
      cell(
        `${shortAddress(from?.address_or_identifier, 4, 3)}${relationshipSeparator(edge.direction)}${shortAddress(to?.address_or_identifier, 4, 3)}`,
      ),
      cell(badge(edge.strength, edge.strength === "weak" ? "yellow" : "blue")),
      cell(evidenceState),
    );
    body.append(row);
  }
  table.append(head, body);
  const wrapper = node("div", "table-scroll");
  wrapper.append(table);
  target.append(wrapper);
}
function downloadJSON(value, filename) {
  if (!value) {
    notify("No export is available.");
    return;
  }
  const url = URL.createObjectURL(
      new Blob([JSON.stringify(value, null, 2) + "\n"], {
        type: "application/json",
      }),
    ),
    link = node("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
  notify("Public evidence export downloaded.");
}
async function exportRows(kind) {
  const version = state.version;
  const source = state.source;
  try {
    const page = await read(kind, {
      ...optionsFor(kind),
      limit: 100,
      offset: 0,
    });
    if (version !== state.version) return;
    downloadJSON(
      {
        ...page,
        source: source === "demo" ? "sample_data" : "local_index",
        redacted: true,
      },
      `quantumorb-${source === "demo" ? "sample-" : ""}${kind}.json`,
    );
  } catch (error) {
    notify(error.message);
  }
}
async function exportGraph() {
  const version = state.version;
  const source = state.source;
  try {
    const graph =
      source === "demo"
        ? demo.graph(null, 200)
        : await api("/graph/export?limit=200");
    if (version !== state.version) return;
    downloadJSON(
      graph,
      `quantumorb-${source === "demo" ? "sample-" : ""}graph.json`,
    );
  } catch (error) {
    notify(error.message);
  }
}
async function refresh() {
  if (state.loading) return;
  state.loading = true;
  const version = state.version;
  try {
    const snapshot =
      state.source === "demo" ? demo.snapshot() : await api("/api/dashboard");
    if (version !== state.version) return;
    renderSnapshot(snapshot);
    if (state.route === "tokens") await loadPage("tokens");
    else if (state.route === "traders") await loadPage("wallets");
    else if (state.route === "activity") await loadPage("activity");
  } catch (error) {
    if (version !== state.version) return;
    setNotice(
      "error",
      `${state.snapshot ? "Refresh failed. Previous values remain visible; check their observation times. " : "Dashboard unavailable. "}${error.message}`,
      "TRY SAMPLE DATA",
      () => setSource("demo"),
    );
    $("header-status").textContent = "REFRESH FAILED ↗";
  } finally {
    if (version === state.version) state.loading = false;
  }
}
function setSource(source) {
  state.source = source === "demo" ? "demo" : "local";
  state.version += 1;
  state.pages = {};
  state.loading = false;
  state.offsets = { tokens: 0, wallets: 0, activity: 0 };
  for (const controller of Object.values(state.requests)) controller.abort();
  if (dialog.open) dialog.close();
  $("data-source").value = state.source;
  try {
    localStorage.setItem("quantumorb.source", state.source);
  } catch {}
  const url = new URL(location.href);
  url.searchParams.set("source", state.source);
  history.replaceState(null, "", url);
  refresh();
  navigate();
}
function resetAndLoad(kind) {
  state.offsets[kind] = 0;
  loadPage(kind);
}
function debounce(callback) {
  let timer;
  return () => {
    clearTimeout(timer);
    timer = setTimeout(callback, 250);
  };
}
for (const [id, kind] of [
  ["token-search", "tokens"],
  ["wallet-search", "wallets"],
  ["activity-search", "activity"],
])
  $(id).addEventListener(
    "input",
    debounce(() => resetAndLoad(kind)),
  );
for (const [id, kind] of [
  ["lifecycle-filter", "tokens"],
  ["wallet-window", "wallets"],
  ["activity-window", "activity"],
  ["finality-filter", "activity"],
])
  $(id).addEventListener("change", () => resetAndLoad(kind));
document
  .querySelectorAll("[data-export]")
  .forEach((button) =>
    button.addEventListener("click", () => exportRows(button.dataset.export)),
  );
$("export-graph").addEventListener("click", exportGraph);
$("refresh-now").addEventListener("click", () => {
  refresh();
  notify("Refreshing the local snapshot…");
});
$("data-source").value = state.source;
$("data-source").addEventListener("change", () =>
  setSource($("data-source").value),
);
$("pause-feed").addEventListener("click", () => {
  state.paused = !state.paused;
  $("pause-feed").setAttribute("aria-pressed", String(state.paused));
  $("pause-feed").setAttribute(
    "aria-label",
    state.paused
      ? "Resume automatic data refresh"
      : "Pause automatic data refresh",
  );
  $("pause-feed").textContent = state.paused ? "▶ RESUME" : "Ⅱ PAUSE";
  if (!state.paused) refresh();
});
$("menu-toggle").addEventListener("click", () => {
  const open = $("main-nav").classList.toggle("open");
  $("menu-toggle").setAttribute("aria-expanded", String(open));
});
document.addEventListener("keydown", (event) => {
  if (
    event.key === "/" &&
    !dialog.open &&
    !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)
  ) {
    event.preventDefault();
    if (!["tokens", "traders", "activity"].includes(state.route))
      location.hash = "tokens";
    requestAnimationFrame(() =>
      $(
        state.route === "traders"
          ? "wallet-search"
          : state.route === "activity"
            ? "activity-search"
            : "token-search",
      ).focus(),
    );
  }
});
window.addEventListener("hashchange", navigate);
setInterval(() => {
  if (!state.paused && !document.hidden) refresh();
}, 10000);
navigate();
refresh();
