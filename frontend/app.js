/* FinTrust risk desk: plain JavaScript, no libraries, no build step.
 *
 * Every number on screen comes from the real API (API Gateway -> query Lambda).
 * Nothing is mocked.
 *
 * Safety rules followed everywhere in this file:
 *   1. Text from the API is always put on the page with textContent, never as
 *      HTML, so a value such as a merchant name can never run as script.
 *   2. People sign in with Amazon Cognito (auth.js). This file never sees a
 *      password; it sends the ID token with each request, and the API checks
 *      the token, the session and the person's role on every call.
 *   3. What a person may do is decided by the API. The page only hides buttons
 *      the API would refuse, so nobody is offered an action they cannot take.
 *
 * Where you are is kept in the address (for example #/transactions?sort=amount_desc),
 * so the Back button, a page refresh and a shared link all work.
 */
"use strict";

(function () {
  const CONFIG = window.FINTRUST_CONFIG || {};
  const API_URL = (CONFIG.apiUrl || "").replace(/\/+$/, "");
  const API_KEY = CONFIG.apiKey || "";
  const PAGE_SIZE = 25;
  const TRACK_LIMIT = 12;            // transactions followed live after an upload
  const TRACK_EVERY_MS = 4000;       // gentle on the API's rate limit (10 requests a second)
  const TRACK_FOR_MS = 4 * 60 * 1000;
  const MAX_UPLOAD_BYTES = 5 * 1024 * 1024;
  const FINISHED = ["SCORED", "ALERTED", "DEAD_LETTERED"];
  const NOTICE = "fintrust.notice";
  const HEARTBEAT_AFTER_MS = 4 * 60 * 1000;   // an active person's session is kept open on the server
  const WARN_BEFORE_MS = 60 * 1000;           // the idle warning appears a minute before sign-out

  const STAGE_NAMES = {
    RECEIVED: "Received", VALIDATED: "Passed validation", QUEUED: "Queued for scoring",
    SCORE_FAILED: "Scoring attempt failed", SCORED: "Scored and stored in the ledger",
    ALERT_SENT: "Alert emailed to compliance", ALERT_FAILED: "Alert email failed",
  };
  const STATUS_NAMES = {
    RECEIVED: "Received", QUEUED: "Queued", RETRYING: "Retrying", SCORED: "Scored",
    ALERTED: "Scored, alert sent", DEAD_LETTERED: "In the dead-letter queue",
  };
  const RULE_NAMES = { HIGH_AMOUNT: "High amount", HIGH_VELOCITY: "High velocity", CROSS_BORDER: "Cross-border" };
  const ROLE_NAMES = { "compliance-officer": "Compliance officer", "compliance-analyst": "Compliance analyst" };
  const VIEWS = ["overview", "transactions", "alerts", "accounts", "upload"];

  const state = {
    session: null,                    // GET /session: who is signed in, role, permissions
    view: null, update: null,         // the view on screen, and how to refresh it in place
    tracker: null,
    balanceAccount: null,             // the account whose running balance is shown
    lastApi: Date.now(), lastActivity: Date.now(), idleTimer: null, warnTimer: null, endingSession: false,
  };
  const $ = (id) => document.getElementById(id);

  // ------------------------------------------------------------------ helpers

  /** Build an element. Children may be elements or plain text (added safely as text). */
  function el(tag, attributes, ...children) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attributes || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (name === "class") node.className = value;
      else if (name === "text") node.textContent = value;
      else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
      else node.setAttribute(name, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  /** Small line icons, drawn as SVG paths (24 x 24 grid). */
  const ICONS = {
    sun: ["M12 4V2M12 22v-2M4 12H2M22 12h-2M5.6 5.6 4.2 4.2M19.8 19.8l-1.4-1.4M5.6 18.4l-1.4 1.4M19.8 4.2l-1.4 1.4",
      "M12 16.5a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9Z"],
    moon: ["M20.5 14.2A8.5 8.5 0 0 1 9.8 3.5 8.5 8.5 0 1 0 20.5 14.2Z"],
    up: ["M7 14l5-5 5 5"],
    down: ["M7 10l5 5 5-5"],
    updown: ["M8 9.5l4-4 4 4", "M8 14.5l4 4 4-4"],
    close: ["M6 6l12 12M18 6 6 18"],
    filter: ["M4 6h16M7 12h10M10 18h4"],
  };
  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    svg.setAttribute("class", "icon");
    for (const d of ICONS[name]) {
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", d);
      svg.append(path);
    }
    return svg;
  }

  const money = new Intl.NumberFormat("en-ZA", { style: "currency", currency: "ZAR" });
  const whole = new Intl.NumberFormat("en-ZA");
  const dateTime = new Intl.DateTimeFormat("en-ZA", {
    timeZone: "Africa/Johannesburg", year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
  const timeOnly = new Intl.DateTimeFormat("en-ZA", {
    timeZone: "Africa/Johannesburg", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
  const clock = new Intl.DateTimeFormat("en-ZA", { timeZone: "Africa/Johannesburg", hour: "2-digit", minute: "2-digit" });

  function formatMoney(value) {
    const number = Number(value);
    return Number.isFinite(number) ? money.format(number) : "";
  }

  function formatDate(value, formatter) {
    if (!value) return "";
    // PostgreSQL text looks like "2026-10-02 09:48:09+00:00"; make it standard ISO.
    const parsed = new Date(String(value).replace(" ", "T"));
    return Number.isNaN(parsed.getTime()) ? String(value) : (formatter || dateTime).format(parsed);
  }

  function riskPill(level) {
    const known = { LOW: "low", MEDIUM: "medium", HIGH: "high" }[level];
    return el("span", { class: "pill " + (known || "plain"), text: level ? level.charAt(0) + level.slice(1).toLowerCase() : "" });
  }

  function toast(message) {
    const box = $("toast");
    box.textContent = message;
    box.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { box.hidden = true; }, 4500);
  }

  /** Tell a screen reader what changed (for example "Sorted by amount, high to low"). */
  function announce(message) {
    const box = $("announcer");
    box.textContent = "";
    setTimeout(() => { box.textContent = message; }, 60);
  }

  const titleCase = (word) => word ? word.charAt(0) + word.slice(1).toLowerCase() : "";

  // ---------------------------------------------------------------------- API

  class ApiFailure extends Error {
    constructor(status, message, requestId, code, reason) {
      super(message);
      this.status = status;
      this.requestId = requestId;
      this.code = code;
      this.reason = reason;
    }
  }

  /**
   * Call the API with the dashboard's API key and the person's ID token.
   * Returns the parsed JSON body, or throws ApiFailure with a message fit to show.
   */
  async function api(method, path, options) {
    const { params, body, csv, retried } = options || {};
    const url = new URL(API_URL + path);
    for (const [name, value] of Object.entries(params || {})) {
      if (value !== "" && value !== null && value !== undefined) url.searchParams.set(name, value);
    }
    let token;
    try {
      token = await FinTrustAuth.idToken();
    } catch (error) {
      throw new ApiFailure(401, error.message, null, "NOT_SIGNED_IN", "EXPIRED");
    }
    const headers = { "x-api-key": API_KEY, Authorization: token };
    let payload;
    if (csv !== undefined) {
      headers["Content-Type"] = "text/csv";
      payload = csv;
    } else if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }

    let response;
    try {
      response = await fetch(url, { method, headers, body: payload, cache: "no-store" });
    } catch (_error) {
      throw new ApiFailure(0, "The API could not be reached. Check your connection and try again.");
    }

    let data = null;
    try { data = await response.json(); } catch (_error) { /* an empty or non-JSON body */ }
    if (response.ok) {
      state.lastApi = Date.now();
      return data;
    }

    const error = (data && data.error) || {};
    if (response.status === 401) {
      if (error.code === "SESSION_ENDED") throw new ApiFailure(401, error.message, error.request_id, error.code, error.reason);
      // API Gateway refused the token itself (it expired early, or was replaced): renew it once and retry.
      if (!retried) {
        try { await FinTrustAuth.idToken(true); } catch (_error) {
          throw new ApiFailure(401, "Your sign-in has ended. Sign in again.", null, "NOT_SIGNED_IN", "EXPIRED");
        }
        return api(method, path, { ...options, retried: true });
      }
      throw new ApiFailure(401, error.message || "Sign in again.", error.request_id, "NOT_SIGNED_IN", "EXPIRED");
    }
    if (response.status === 403 && !error.code) {
      throw new ApiFailure(403, "This dashboard's access key was not accepted. An administrator needs to redeploy the dashboard.",
        null, "BAD_KEY");
    }
    if (response.status === 429) throw new ApiFailure(429, "Too many requests. Wait a moment and try again.");
    throw new ApiFailure(response.status, error.message || (data && data.message) || `The API answered ${response.status}.`,
      error.request_id, error.code);
  }

  function failureNotice(error) {
    const reference = error.requestId ? ` Reference: ${error.requestId}` : "";
    return el("p", { class: "notice error", role: "alert", text: error.message + reference });
  }

  /** A 401 at any point means the session is over: send the person to sign in, saying why. */
  function handleFailure(error, container) {
    if (error.status === 401) {
      endSession(error.reason || "EXPIRED", error.message);
      return;
    }
    container.replaceChildren(failureNotice(error));
  }

  // ---------------------------------------------------------------- sign-in

  const NOTICES = {
    SIGNED_OUT: "You have signed out. Your session has ended on this device and on the server.",
    IDLE: "For your security you were signed out after a period without activity.",
    EXPIRED: "Your session reached its time limit. Sign in again to continue.",
  };

  function takeNotice() {
    try {
      const notice = JSON.parse(window.sessionStorage.getItem(NOTICE) || "null");
      window.sessionStorage.removeItem(NOTICE);
      return notice;
    } catch (_error) {
      return null;
    }
  }

  function showSignIn(notice) {
    stopIdleWatch();
    stopTracking();
    closeDrawer();
    closeUserMenu();
    state.session = null;
    $("app-view").hidden = true;
    $("user").hidden = true;
    $("signin-view").hidden = false;
    const box = $("signin-notice");
    box.hidden = !notice;
    box.textContent = notice ? notice.message : "";
    box.className = "notice" + (notice && notice.tone ? " " + notice.tone : "");
    const button = $("sign-in");
    button.disabled = !FinTrustAuth.configured() || !API_URL;
    if (button.disabled) {
      box.hidden = false;
      box.className = "notice error";
      box.textContent = "Sign-in is not set up for this dashboard yet. An administrator needs to run deploy_auth.sh and redeploy the dashboard.";
    }
    document.title = "Sign in · FinTrust risk desk";
    checkSystem();
  }

  /** The sign-in page says whether the system is up, so nobody signs in to a stopped database. */
  async function checkSystem() {
    const line = $("system-status");
    if (!API_URL) { line.replaceChildren(); return; }
    line.replaceChildren(el("span", { class: "dot", "aria-hidden": "true" }), "Checking the system…");
    line.className = "signin-status";
    let status = 0;
    try {
      const response = await fetch(API_URL + "/health", { headers: { "x-api-key": API_KEY }, cache: "no-store" });
      status = response.status;
    } catch (_error) { /* unreachable */ }
    const [tone, text] = status === 200 ? ["good", "System online. The ledger is available."]
      : status === 503 ? ["warn", "The database is stopped or starting (it is paused outside working hours). You can sign in; data appears once it is running."]
        : ["bad", "The risk API cannot be reached right now."];
    line.className = "signin-status " + tone;
    line.replaceChildren(el("span", { class: "dot", "aria-hidden": "true" }), text);
  }

  async function beginSignIn() {
    const button = $("sign-in");
    button.disabled = true;
    button.textContent = "Opening sign-in…";
    try {
      const target = window.location.hash && window.location.hash !== "#/" ? window.location.hash : "";
      await FinTrustAuth.signIn(target);
    } catch (error) {
      button.disabled = false;
      button.textContent = "Sign in to the risk desk";
      showSignIn({ message: error.message, tone: "error" });
    }
  }

  /**
   * End the session: on the server (DELETE /session), at Cognito (the refresh
   * token is revoked and the sign-in cookie cleared), and in this tab. The
   * reason is shown on the sign-in page the browser comes back to.
   */
  async function endSession(reason, message) {
    if (state.endingSession) return;
    state.endingSession = true;
    stopIdleWatch();
    stopTracking();
    try {
      window.sessionStorage.setItem(NOTICE, JSON.stringify({
        message: message || NOTICES[reason] || NOTICES.EXPIRED, tone: reason === "SIGNED_OUT" ? "good" : null }));
    } catch (_error) { /* the notice is a courtesy */ }
    if (["SIGNED_OUT", "IDLE"].includes(reason) && FinTrustAuth.hasTokens()) {
      try { await api("DELETE", "/session"); } catch (_error) { /* the session may already be over */ }
    }
    await FinTrustAuth.signOut();
  }

  function enterApp(session) {
    state.session = session;
    state.endingSession = false;
    $("signin-view").hidden = true;
    $("app-view").hidden = false;
    showUser(session);
    startIdleWatch();
    render();
    refreshAlertCount();
  }

  async function start() {
    applyThemeButton();
    if (!API_URL || !FinTrustAuth.configured()) { showSignIn(null); return; }

    let returned = null;
    try {
      returned = await FinTrustAuth.completeSignIn();
    } catch (error) {
      showSignIn({ message: error.message, tone: "error" });
      return;
    }
    if (!FinTrustAuth.hasTokens()) { showSignIn(takeNotice()); return; }

    $("signin-view").hidden = true;
    try {
      // A new sign-in starts a session; a page refresh checks the existing one is still open.
      const session = returned ? await api("POST", "/session") : await api("GET", "/session");
      if (returned && returned.returnTo) window.history.replaceState(null, "", returned.returnTo);
      enterApp(session);
    } catch (error) {
      if (error.status === 401 || error.code === "NO_ROLE") { endSession(error.reason || "EXPIRED", error.message); return; }
      showSignIn({ message: error.message, tone: "error" });
    }
  }

  // ------------------------------------------------------- idle and heartbeat

  function noteActivity() {
    if ($("idle-dialog").open) return;          // while the warning shows, only its buttons count
    state.lastActivity = Date.now();
  }

  function startIdleWatch() {
    state.lastActivity = Date.now();
    clearInterval(state.idleTimer);
    state.idleTimer = setInterval(checkIdle, 15000);
  }

  function stopIdleWatch() {
    clearInterval(state.idleTimer);
    clearInterval(state.warnTimer);
    state.idleTimer = null;
    if ($("idle-dialog").open) $("idle-dialog").close();
  }

  function idleLimit() {
    return ((state.session && state.session.idle_minutes) || 15) * 60 * 1000;
  }

  async function heartbeat() {
    if (state.heartbeat) return;                     // one at a time
    state.heartbeat = true;
    try {
      state.session = { ...state.session, ...(await api("GET", "/session")) };
    } catch (error) {
      if (error.status === 401) endSession(error.reason || "EXPIRED", error.message);
    } finally {
      state.heartbeat = false;
    }
  }

  function checkIdle() {
    if (!state.session) return;
    const now = Date.now();
    const idleFor = now - state.lastActivity;
    if (idleFor >= idleLimit()) { endSession("IDLE"); return; }
    if (idleFor >= idleLimit() - WARN_BEFORE_MS) { showIdleWarning(); return; }
    // The server ends a session after the same quiet time. Someone reading a
    // chart makes no requests, so the page tells the server they are still here.
    if (now - state.lastApi > HEARTBEAT_AFTER_MS && state.lastActivity > state.lastApi) heartbeat();
  }

  function showIdleWarning() {
    const dialog = $("idle-dialog");
    if (dialog.open) return;
    const tick = () => {
      const left = Math.max(0, Math.ceil((idleLimit() - (Date.now() - state.lastActivity)) / 1000));
      $("idle-text").textContent = `For your security you will be signed out in ${left} second${left === 1 ? "" : "s"}.`;
      if (left === 0) endSession("IDLE");
    };
    tick();
    clearInterval(state.warnTimer);
    state.warnTimer = setInterval(tick, 1000);
    dialog.showModal();
    $("idle-stay").focus();
  }

  function stayActive() {
    clearInterval(state.warnTimer);
    $("idle-dialog").close();
    state.lastActivity = Date.now();
    heartbeat();
  }

  // ------------------------------------------------------- masthead and menu

  function applyThemeButton() {
    const button = $("theme-toggle");
    const dark = FinTrustTheme.effective() === "dark";
    button.replaceChildren(icon(dark ? "sun" : "moon"));
    button.setAttribute("aria-label", dark ? "Switch to light appearance" : "Switch to dark appearance");
    button.title = dark ? "Light appearance" : "Dark appearance";
    for (const input of document.querySelectorAll("#appearance input")) input.checked = input.value === FinTrustTheme.choice();
  }

  function initials(name) {
    const parts = String(name || "").trim().split(/\s+/).filter(Boolean);
    return ((parts[0] || "?").charAt(0) + (parts.length > 1 ? parts[parts.length - 1].charAt(0) : "")).toUpperCase();
  }

  function roleName(session) {
    return (session.roles || []).map((role) => ROLE_NAMES[role] || role).join(", ") || "No role";
  }

  function showUser(session) {
    $("user").hidden = false;
    $("user-initials").textContent = initials(session.name);
    $("user-name").textContent = session.name;
    $("user-menu-name").textContent = session.name;
    $("user-menu-username").textContent = session.username;
    $("user-menu-role").replaceChildren(el("span", { class: "role-badge" + (state.session.permissions.resolve_alerts ? " officer" : ""),
      text: roleName(session) }));
    const started = formatDate(session.started_at, clock);
    const ends = formatDate(session.ends_at, clock);
    $("user-menu-session").textContent =
      `Signed in at ${started}. You are signed out after ${session.idle_minutes} minutes without activity, and at ${ends} at the latest.`;
    $("user-button").setAttribute("aria-label", `${session.name}, ${roleName(session)}. Account menu`);
  }

  function openUserMenu() {
    $("user-menu").hidden = false;
    $("user-button").setAttribute("aria-expanded", "true");
    const checked = document.querySelector("#appearance input:checked") || document.querySelector("#appearance input");
    checked.focus();
  }

  function closeUserMenu(returnFocus) {
    if ($("user-menu").hidden) return;
    $("user-menu").hidden = true;
    $("user-button").setAttribute("aria-expanded", "false");
    if (returnFocus) $("user-button").focus();
  }

  // ------------------------------------------------------------------ routing

  function readAddress() {
    const raw = window.location.hash.replace(/^#\/?/, "");
    const [name, query] = raw.split("?");
    const view = VIEWS.includes(name) ? name : "overview";
    const params = {};
    for (const [key, value] of new URLSearchParams(query || "")) if (value !== "") params[key] = value;
    return { view, params };
  }

  /** Go to a view with these parameters. The address changes, so Back returns here. */
  function navigate(view, params) {
    const clean = Object.fromEntries(Object.entries(params || {}).filter(([, value]) => value !== "" && value !== null && value !== undefined));
    const query = new URLSearchParams(clean).toString();
    const target = "#/" + view + (query ? "?" + query : "");
    if (window.location.hash === target) render();
    else window.location.hash = target;
  }

  const views = {};

  function render() {
    if (!state.session) return;
    const { view, params } = readAddress();
    FinTrustCharts.hideTip();
    for (const tab of document.querySelectorAll(".tab")) {
      if (tab.dataset.view === view) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    }
    document.title = `${$("app-view").querySelector(`.tab[data-view="${view}"]`).firstChild.textContent.trim()} · FinTrust risk desk`;
    if (state.view === view && state.update) {
      state.update(params);                  // same view: refresh in place, nothing jumps, focus stays
      return;
    }
    stopTracking();
    state.view = view;
    state.update = null;
    const page = $("page");
    page.replaceChildren(el("p", { class: "notice", text: "Loading…" }));
    state.update = views[view](page, params) || null;
    window.scrollTo(0, 0);
    page.focus({ preventScroll: true });
  }

  async function refreshAlertCount() {
    try {
      const data = await api("GET", "/alerts", { params: { status: "OPEN", limit: 1 } });
      const badge = $("alert-count");
      badge.textContent = whole.format(data.pagination.total);
      badge.hidden = data.pagination.total === 0;
      badge.setAttribute("aria-label", `${data.pagination.total} open`);
    } catch (_error) { /* the badge is a convenience; the Alerts view reports real errors */ }
  }

  // ----------------------------------------------------------- shared pieces

  function pageHead(title, lede, ...extra) {
    return el("div", { class: "page-head" },
      el("div", null, el("h1", { text: title }), lede && el("p", { class: "lede", text: lede })), ...extra);
  }

  /**
   * A table. Columns: {label, cell(row), number, wrap, sort: {asc, desc, first}}.
   * `sorting` = {current, onSort(key)} makes the headers of sortable columns into buttons.
   */
  function table(columns, rows, emptyText, onRowClick, sorting) {
    if (!rows.length) return el("div", { class: "table-wrap" }, el("p", { class: "empty", text: emptyText }));
    const head = el("tr", null, columns.map((c) => {
      const classes = c.number ? "number" : null;
      if (!c.sort || !sorting) return el("th", { class: classes, scope: "col", text: c.label });
      const direction = sorting.current === c.sort.desc ? "descending" : sorting.current === c.sort.asc ? "ascending" : "none";
      const next = direction === "none" ? c.sort[c.sort.first || "desc"]
        : direction === "descending" ? (c.sort.asc || c.sort.desc) : (c.sort.desc || c.sort.asc);
      const symbol = direction === "descending" ? "down" : direction === "ascending" ? "up" : "updown";
      return el("th", { class: [classes, "sortable", direction !== "none" ? "sorted" : ""].filter(Boolean).join(" "),
        scope: "col", "aria-sort": direction },
      el("button", { type: "button", class: "sort-button", "data-key": "sort-" + c.label,
        title: `Sort by ${c.label.toLowerCase()}`, onclick: () => sorting.onSort(next) }, c.label, icon(symbol)));
    }));
    const body = rows.map((row) => {
      const cells = columns.map((c) => el("td", { class: [c.number ? "number" : "", c.wrap ? "wrap" : ""].join(" ").trim() || null }, c.cell(row)));
      const line = el("tr", onRowClick ? { class: "clickable", tabindex: "0" } : null, cells);
      if (onRowClick) {
        line.addEventListener("click", (event) => { if (!event.target.closest("button")) onRowClick(row); });
        line.addEventListener("keydown", (event) => { if (event.key === "Enter" && event.target === line) onRowClick(row); });
      }
      return line;
    });
    return el("div", { class: "table-wrap" }, el("table", null, el("thead", null, head), el("tbody", null, body)));
  }

  function pager(pagination, onMove) {
    const first = pagination.total ? pagination.offset + 1 : 0;
    const last = Math.min(pagination.offset + pagination.limit, pagination.total);
    return el("div", { class: "pager" },
      el("span", { text: `${whole.format(first)} to ${whole.format(last)} of ${whole.format(pagination.total)}` }),
      el("div", null,
        el("button", { type: "button", class: "button small", "data-key": "previous", disabled: pagination.offset === 0,
          onclick: () => onMove(Math.max(0, pagination.offset - pagination.limit)) }, "Previous"),
        el("button", { type: "button", class: "button small", "data-key": "next", disabled: !pagination.has_more,
          onclick: () => onMove(pagination.offset + pagination.limit) }, "Next")));
  }

  let fieldCount = 0;
  function field(label, input, hint) {
    fieldCount += 1;
    const id = "f-" + fieldCount;
    input.id = id;
    let hintNode = null;
    if (hint) {
      hintNode = el("span", { class: "hint", id: id + "-hint", text: hint });
      input.setAttribute("aria-describedby", id + "-hint");
    }
    return el("div", { class: "field" }, el("label", { for: id, text: label }), input, hintNode);
  }

  function select(key, options, value, label) {
    return el("select", { "data-key": key, "aria-label": label || null },
      options.map(([optionValue, text]) => el("option", { value: optionValue, selected: optionValue === (value || "") }, text)));
  }

  /** Put the focus back on the control that had it before the page was redrawn. */
  function keepFocus(redraw) {
    const key = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.key : null;
    redraw();
    if (key) {
      const again = document.querySelector(`[data-key="${CSS.escape(key)}"]`);
      if (again) again.focus({ preventScroll: true });
    }
  }

  /**
   * The list views (transactions, alerts, accounts) share one shape: a filter
   * form at the top, a line of active filters, then the results. The form
   * reflects the address; changing it changes the address.
   *
   * spec = {view, defaults, sorts: [[value, label]], primary(query, apply), advanced(query, apply),
   *         chips(query) -> [{label, keys}], load(query, results)}
   */
  function listView(page, params, spec) {
    let query = { ...spec.defaults, ...params };
    const apply = (changes, keepOffset) => navigate(spec.view, { ...query, ...changes, ...(keepOffset ? {} : { offset: "" }) });
    const head = el("div");
    const results = el("div", { class: "results", "aria-live": "off" });
    page.replaceChildren(pageHead(spec.title, spec.lede), head, results);

    function draw() {
      const advancedOpen = head.querySelector(".more-filters") ? !head.querySelector(".more-filters").hidden : false;
      const form = el("form", { class: "filters", role: "search", "aria-label": `Filter ${spec.title.toLowerCase()}` });
      const primary = spec.primary(query, apply);
      const sort = select("sort", spec.sorts, query.sort);
      sort.dataset.param = "sort";
      sort.addEventListener("change", () => {
        form.requestSubmit();
        announce("Sorted by " + spec.sorts.find(([value]) => value === sort.value)[1]);
      });
      const advanced = spec.advanced ? spec.advanced(query, apply) : null;
      const advancedCount = advanced ? advanced.active : 0;
      const more = advanced ? el("div", { class: "more-filters", id: spec.view + "-more", hidden: !(advancedOpen || advancedCount) }, advanced.fields) : null;
      const moreButton = advanced ? el("button", { type: "button", class: "button ghost", "data-key": "more",
        "aria-expanded": String(!more.hidden), "aria-controls": spec.view + "-more",
        onclick: () => { more.hidden = !more.hidden; moreButton.setAttribute("aria-expanded", String(!more.hidden)); } },
      icon("filter"), advancedCount ? `More filters (${advancedCount})` : "More filters") : null;
      const sortField = field("Sort by", sort);
      sortField.classList.add("sort-field");
      form.append(...primary, sortField,
        el("div", { class: "filter-actions" }, el("button", { type: "submit", class: "button primary", "data-key": "apply" }, "Apply"), moreButton));
      if (more) form.append(more);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const values = {};
        for (const control of form.querySelectorAll("[data-param]")) values[control.dataset.param] = control.value.trim();
        const problem = spec.check ? spec.check(values) : null;
        if (problem) { toast(problem); return; }
        apply(values);
      });

      const chips = spec.chips(query).filter(Boolean);
      const chipRow = chips.length ? el("div", { class: "chips", role: "group", "aria-label": "Active filters" },
        chips.map((chip) => el("button", { type: "button", class: "chip removable", "data-key": "chip-" + chip.keys.join("-"),
          "aria-label": `Remove filter ${chip.label}`,
          onclick: () => apply(Object.fromEntries(chip.keys.map((key) => [key, spec.cleared && key in spec.cleared ? spec.cleared[key] : ""]))) },
        chip.label, icon("close"))),
        el("button", { type: "button", class: "link-button", "data-key": "clear-all",
          onclick: () => navigate(spec.view, query.sort && query.sort !== spec.defaults.sort ? { sort: query.sort } : {}) }, "Clear all filters"))
        : null;
      head.replaceChildren(form, chipRow || "");
    }

    function update(next) {
      query = { ...spec.defaults, ...next };
      keepFocus(draw);
      results.classList.add("refreshing");
      spec.load(query, results, apply);
    }
    update(params);
    return update;
  }

  /** The line above a results table: how many, and in what order. */
  function resultsSummary(total, noun, sorts, sort) {
    const label = (sorts.find(([value]) => value === sort) || [null, ""])[1];
    return el("p", { class: "results-summary" },
      el("strong", { text: `${whole.format(total)} ${noun}${total === 1 ? "" : "s"}` }),
      label ? ` · sorted by ${label.charAt(0).toLowerCase() + label.slice(1)}` : "");
  }

  function sortingFor(query, apply, sorts) {
    return {
      current: query.sort,
      onSort: (key) => {
        apply({ sort: key });
        announce("Sorted by " + (sorts.find(([value]) => value === key) || [null, key])[1]);
      },
    };
  }

  /** An amount for a filter label: whole rands without cents ("R 50 000"), otherwise with them. */
  const wholeRand = new Intl.NumberFormat("en-ZA", { style: "currency", currency: "ZAR", maximumFractionDigits: 0 });
  const amountText = (value) => (Number(value) % 1 === 0 ? wholeRand.format(Number(value)) : formatMoney(value));

  // ----------------------------------------------------------------- overview

  // The Overview answers three questions, top to bottom:
  //   how much was processed and what needs attention   (the figures, GET /insights)
  //   how it changed over time and how scores spread    (the charts, GET /insights/trends)
  //   which accounts stand out, and what their money did (ranking + running balance)
  // Every chart is drawn from numbers PostgreSQL worked out; see docs/dashboard.md.

  views.overview = function (page, params) {
    const from = el("input", { type: "date", "data-key": "from" });
    const to = el("input", { type: "date", "data-key": "to" });
    const presetDays = [null, 7, 30, 90];
    const presets = el("div", { class: "presets", role: "group", "aria-label": "Quick periods" },
      [["All time", null], ["Last 7 days", 7], ["Last 30 days", 30], ["Last 90 days", 90]].map(([label, days]) =>
        el("button", { type: "button", class: "chip", "data-key": "preset-" + label,
          onclick: () => navigate("overview", days ? lastDays(days) : {}) }, label)));
    const filters = el("form", { class: "filters", onsubmit: (event) => {
      event.preventDefault();
      if (from.value && to.value && from.value > to.value) { toast("The From date must not be after the To date."); return; }
      navigate("overview", { from: from.value, to: to.value });
    } }, field("From", from), field("To", to), el("button", { type: "submit", class: "button", "data-key": "show" }, "Show period"), presets);

    const content = el("div", { class: "overview" }, el("p", { class: "notice", text: "Loading…" }));
    page.replaceChildren(
      pageHead("Overview", "What has been processed and what needs attention. Leave the dates empty to see everything."),
      filters, content);

    // A new period refreshes the same page in place (the filters, presets and
    // charts stay where they are), so nothing jumps while the data loads.
    function update(next) {
      const period = { from: next.from || "", to: next.to || "" };
      state.period = period;
      from.value = period.from;
      to.value = period.to;
      presets.querySelectorAll(".chip").forEach((chip, i) => chip.setAttribute("aria-pressed", String(isPreset(period, presetDays[i]))));
      loadOverview(content, period);
    }
    update(params);
    return update;
  };

  function isPreset(period, days) {
    if (!days) return !period.from && !period.to;
    const preset = lastDays(days);
    return period.from === preset.from && period.to === preset.to;
  }

  /** The last `days` calendar days, ending today in South African time, as YYYY-MM-DD. */
  function lastDays(days) {
    const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Africa/Johannesburg" }).format(new Date());
    const start = new Date(today + "T00:00:00Z");
    start.setUTCDate(start.getUTCDate() - (days - 1));
    return { from: start.toISOString().slice(0, 10), to: today };
  }

  async function loadOverview(content, period) {
    // Keep the previous picture, dimmed, until the new one arrives: no jump, no flash.
    content.classList.add("refreshing");
    const params = { from: period.from, to: period.to };
    // Asked for together; if the charts fail, the figures still show (and the other way round).
    const [summary, trends] = await Promise.allSettled([
      api("GET", "/insights", { params }),
      api("GET", "/insights/trends", { params }),
    ]);
    if (state.period !== period || !content.isConnected) return;       // the user has moved on
    content.classList.remove("refreshing");
    const failed = [summary, trends].find((result) => result.status === "rejected" && result.reason.status === 401);
    if (failed) { handleFailure(failed.reason, content); return; }
    if (summary.status === "rejected") { handleFailure(summary.reason, content); return; }

    const data = summary.value;
    const levels = data.by_risk_level;
    const total = data.totals.transactions;
    const parts = [figures(data)];

    if (trends.status === "rejected") {
      parts.push(el("div", { class: "section" }, failureNotice(trends.reason)));
    } else {
      parts.push(activityPanel(trends.value, period));
    }

    const share = el("div", { class: "share", role: "img",
      "aria-label": `Low ${levels.LOW}, medium ${levels.MEDIUM}, high ${levels.HIGH}` });
    for (const level of ["LOW", "MEDIUM", "HIGH"]) {
      if (!levels[level]) continue;
      const part = el("span", { class: level.toLowerCase(), title: `${titleCase(level)}: ${whole.format(levels[level])}` });
      part.style.flexGrow = String(levels[level]);
      share.append(part);
    }
    const key = el("ul", { class: "key" }, ["LOW", "MEDIUM", "HIGH"].map((level) => el("li", null,
      el("span", { class: "swatch " + level.toLowerCase(), "aria-hidden": "true" }),
      `${titleCase(level)} ${whole.format(levels[level])}`, el("span", { class: "muted", text: total ? ` (${percent(levels[level] / total)})` : "" }))));

    const mostFired = Math.max(1, ...Object.values(data.rules_fired));
    const rules = el("ul", { class: "bars" }, Object.entries(data.rules_fired).map(([rule, fired]) => {
      const fill = el("div", { class: "bar-fill" });
      fill.style.width = (100 * fired / mostFired) + "%";
      if (!fired) fill.style.minWidth = "0";
      return el("li", null, el("span", { text: RULE_NAMES[rule] || rule }),
        el("div", { class: "bar-track" }, fill), el("span", { class: "value", text: whole.format(fired) }));
    }));

    parts.push(el("div", { class: "split section" },
      panel("Risk levels", "Every transaction is scored 0 to 100 and placed in one level.",
        total ? share : el("p", { class: "notice", text: "No transactions in this period." }), total ? key : null),
      panel("Rules that fired", "How many times each risk rule added points. One transaction can fire more than one rule.", rules)));

    if (trends.status === "fulfilled") {
      parts.push(el("div", { class: "split section" }, scorePanel(trends.value), alertPanel(trends.value, period)));
      const balanceHost = el("div");
      parts.push(el("div", { class: "split wide-right section" },
        rankingPanel(trends.value, balanceHost, period), balanceHost));
    }
    content.replaceChildren(...parts);
  }

  function figures(data) {
    const levels = data.by_risk_level;
    const total = data.totals.transactions;
    return el("div", { class: "figures" },
      figure(whole.format(total), "Transactions"),
      figure(formatMoney(data.totals.total_value), "Total value", { wide: true }),
      figure(whole.format(data.totals.accounts), "Accounts with transactions"),
      figure(whole.format(levels.HIGH), "High risk", { attention: levels.HIGH > 0,
        note: total ? `${percent(levels.HIGH / total)} of transactions` : null }),
      figure(whole.format(data.open_alerts), "Open alerts", { attention: data.open_alerts > 0 }));
  }

  function figure(value, label, options) {
    const { attention, wide, note } = options || {};
    return el("div", { class: ["figure", attention ? "attention" : "", wide ? "wide" : ""].join(" ").trim() },
      el("div", { class: "figure-value", text: value }), el("div", { class: "figure-label", text: label }),
      note ? el("div", { class: "figure-note", text: note }) : null);
  }

  /** A titled card. `lede` explains in one line what the reader is looking at. */
  function panel(title, lede, ...body) {
    return el("section", { class: "panel" }, el("h2", { text: title }), lede ? el("p", { class: "panel-lede", text: lede }) : null, ...body);
  }

  /** The numbers behind a chart, as a table: the accessible and auditable twin of every picture. */
  function numbers(columns, rows) {
    return el("details", { class: "numbers" }, el("summary", { text: "Show the numbers" }), table(columns, rows, "Nothing to show."));
  }

  const percent = (fraction) => new Intl.NumberFormat("en-ZA", { style: "percent", maximumFractionDigits: 1 }).format(fraction);
  const compactCount = (value) => whole.format(value);
  /** Axis money: R400k, R1.2m. Precise enough that two ticks `step` apart never read the same. */
  function compactMoney(value, step) {
    const size = Math.abs(value);
    const sign = value < 0 ? "−" : "";
    const unit = size >= 1e6 && step >= 1e5 ? [1e6, "m"] : size >= 1e3 && step >= 100 ? [1e3, "k"] : [1, ""];
    let digits = 0;                                    // as many decimals as the step itself has (2.5k -> 1)
    while (digits < 2 && Math.abs(Math.round(step / unit[0] * 10 ** digits) - step / unit[0] * 10 ** digits) > 1e-6) digits += 1;
    const number = new Intl.NumberFormat("en-ZA", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
    return `${sign}R${number.format(size / unit[0])}${unit[1]}`;
  }

  // Calendar days from the API ("2026-09-05") are formatted as they are, never shifted by a time zone.
  const asDay = (text) => new Date(text + "T00:00:00Z");
  const dayShort = new Intl.DateTimeFormat("en-ZA", { timeZone: "UTC", day: "numeric", month: "short" });
  const dayLong = new Intl.DateTimeFormat("en-ZA", { timeZone: "UTC", weekday: "short", day: "numeric", month: "short", year: "numeric" });
  const monthName = new Intl.DateTimeFormat("en-ZA", { timeZone: "UTC", month: "short", year: "numeric" });
  const monthLong = new Intl.DateTimeFormat("en-ZA", { timeZone: "UTC", month: "long", year: "numeric" });

  function bucketLabel(text, granularity) {
    return granularity === "month" ? monthName.format(asDay(text)) : dayShort.format(asDay(text));
  }
  function bucketTitle(text, granularity) {
    if (granularity === "month") return monthLong.format(asDay(text));
    return (granularity === "week" ? "Week of " : "") + dayLong.format(asDay(text));
  }
  const BUCKET_WORD = { day: "day", week: "week", month: "month" };

  /** The first and last day of one bar, kept inside the period the user chose. */
  function bucketPeriod(start, granularity, period) {
    const last = asDay(start);
    if (granularity === "week") last.setUTCDate(last.getUTCDate() + 6);
    if (granularity === "month") { last.setUTCMonth(last.getUTCMonth() + 1, 0); }
    let from = start;
    let to = last.toISOString().slice(0, 10);
    if (period.from && from < period.from) from = period.from;
    if (period.to && to > period.to) to = period.to;
    return { from, to };
  }

  function zoomInto(start, granularity, period) {
    FinTrustCharts.hideTip();
    navigate("overview", bucketPeriod(start, granularity, period));
  }

  const RISK_SERIES = [
    { key: "low", name: "Low", cls: "low" },
    { key: "medium", name: "Medium", cls: "medium" },
    { key: "high", name: "High", cls: "high" },
  ];

  function activityPanel(trends, period) {
    const rows = trends.activity;
    const word = BUCKET_WORD[trends.granularity];
    const host = el("div");
    const card = panel("Activity over time",
      `Transactions per ${word}, split by risk level.` + (trends.granularity === "day" ? "" : " Select a bar to zoom into it."),
      FinTrustCharts.legend(RISK_SERIES.map((item) => ({ name: item.name, cls: item.cls }))), host,
      numbers([
        { label: titleCase(word), cell: (r) => bucketTitle(r.period_start, trends.granularity) },
        { label: "Transactions", number: true, cell: (r) => whole.format(r.transactions) },
        { label: "Low", number: true, cell: (r) => whole.format(r.low) },
        { label: "Medium", number: true, cell: (r) => whole.format(r.medium) },
        { label: "High", number: true, cell: (r) => whole.format(r.high) },
        { label: "Value", number: true, cell: (r) => formatMoney(r.total_value) },
      ], rows));
    card.classList.add("section");
    if (!rows.length) { host.replaceWith(el("p", { class: "notice", text: "No transactions in this period." })); return card; }
    FinTrustCharts.columnChart(host, {
      label: `Transactions per ${word} by risk level`,
      labels: rows.map((r) => bucketLabel(r.period_start, trends.granularity)),
      titles: rows.map((r) => bucketTitle(r.period_start, trends.granularity)),
      series: RISK_SERIES,
      stacked: true,
      value: (i, key) => rows[i][key],
      tick: compactCount,
      rows: (i) => [
        { value: whole.format(rows[i].transactions), label: "transactions" },
        { value: formatMoney(rows[i].total_value), label: "in value" },
        ...RISK_SERIES.slice().reverse().map((item) => ({ value: whole.format(rows[i][item.key]), label: item.name, key: "key-" + item.cls })),
      ],
      onSelect: trends.granularity === "day" ? null : (i) => zoomInto(rows[i].period_start, trends.granularity, period),
    });
    return card;
  }

  function scorePanel(trends) {
    const distribution = trends.score_distribution;
    const buckets = distribution.buckets;
    const { medium, high } = distribution.thresholds;
    const host = el("div");
    const card = panel("How scores are spread",
      `Each rule adds fixed points, so scores land on a few values. Medium starts at ${medium}, High at ${high} and raises an alert.`,
      FinTrustCharts.legend(RISK_SERIES.map((item) => ({ name: item.name, cls: item.cls }))), host,
      numbers([
        { label: "Score", cell: (b) => `${b.from} to ${b.to}` },
        { label: "Low", number: true, cell: (b) => whole.format(b.low) },
        { label: "Medium", number: true, cell: (b) => whole.format(b.medium) },
        { label: "High", number: true, cell: (b) => whole.format(b.high) },
      ], buckets));
    FinTrustCharts.columnChart(host, {
      label: "Number of transactions in each score range",
      labels: buckets.map((b) => `${b.from}`),
      titles: buckets.map((b) => `Score ${b.from} to ${b.to}`),
      series: RISK_SERIES,
      stacked: true,
      value: (i, key) => buckets[i][key],
      tick: compactCount,
      rows: (i) => [
        { value: whole.format(buckets[i].low + buckets[i].medium + buckets[i].high), label: "transactions" },
        ...RISK_SERIES.slice().reverse().filter((item) => buckets[i][item.key])
          .map((item) => ({ value: whole.format(buckets[i][item.key]), label: item.name, key: "key-" + item.cls })),
      ],
      markers: [
        { position: medium / distribution.bucket_size, label: `Medium ${medium}` },
        { position: high / distribution.bucket_size, label: `High ${high}` },
      ],
    });
    return card;
  }

  function alertPanel(trends, period) {
    const flow = trends.alert_flow;
    const rows = flow.periods;
    const word = BUCKET_WORD[trends.granularity];
    const series = [{ key: "raised", name: "Raised", cls: "raised" }, { key: "resolved", name: "Resolved", cls: "resolved" }];
    const host = el("div");
    const median = flow.median_hours_to_resolve;
    const facts = el("dl", { class: "inline-facts" },
      el("div", null, el("dt", { text: "Raised" }), el("dd", { text: whole.format(flow.raised) })),
      el("div", null, el("dt", { text: "Resolved" }), el("dd", { text: whole.format(flow.resolved) })),
      el("div", null, el("dt", { text: "Open now" }), el("dd", { text: whole.format(flow.open_now) })),
      el("div", null, el("dt", { text: "Median time to resolve" }),
        el("dd", { text: median === null ? "None resolved" : `${new Intl.NumberFormat("en-ZA", { maximumFractionDigits: 1 }).format(Number(median))} hours` })));
    const card = panel("Alerts raised and resolved", `Is the compliance team keeping up? Alerts per ${word}, by when they were raised and when they were resolved.`,
      facts, FinTrustCharts.legend(series.map((item) => ({ name: item.name, cls: item.cls }))), host,
      numbers([
        { label: titleCase(word), cell: (r) => bucketTitle(r.period_start, trends.granularity) },
        { label: "Raised", number: true, cell: (r) => whole.format(r.raised) },
        { label: "Resolved", number: true, cell: (r) => whole.format(r.resolved) },
      ], rows));
    if (!rows.length) { host.replaceWith(el("p", { class: "notice", text: "No alerts in this period." })); return card; }
    FinTrustCharts.columnChart(host, {
      label: `Alerts raised and resolved per ${word}`,
      labels: rows.map((r) => bucketLabel(r.period_start, trends.granularity)),
      titles: rows.map((r) => bucketTitle(r.period_start, trends.granularity)),
      series,
      stacked: false,
      value: (i, key) => rows[i][key],
      tick: compactCount,
      rows: (i) => series.map((item) => ({ value: whole.format(rows[i][item.key]), label: item.name, key: "key-" + item.cls })),
      onSelect: trends.granularity === "day" ? null : (i) => zoomInto(rows[i].period_start, trends.granularity, period),
    });
    return card;
  }

  function rankingPanel(trends, balanceHost, period) {
    const ranking = trends.account_ranking;
    const items = ranking.items;
    const card = panel("Fraud ranking",
      `Accounts by total risk points. Percentile is PERCENT_RANK over all ${whole.format(ranking.accounts_ranked)} accounts: 1.00 is the riskiest. A burst is ${ranking.burst_rule}.`);
    if (!items.length) {
      card.append(el("p", { class: "notice", text: "No accounts had transactions in this period." }));
      balanceHost.replaceChildren();
      return card;
    }
    const most = Math.max(1, ...items.map((row) => row.total_risk_points));
    const buttons = [];
    const choose = (accountId) => {
      state.balanceAccount = accountId;
      for (const button of buttons) button.setAttribute("aria-pressed", String(button.dataset.account === accountId));
      loadBalance(balanceHost, accountId, period);
    };
    const list = el("ol", { class: "ranking" }, items.map((row) => {
      const fill = el("span", { class: "rank-fill" });
      fill.style.width = (100 * row.total_risk_points / most) + "%";
      const button = el("button", { type: "button", class: "rank-row", "data-account": row.account_id, "aria-pressed": "false",
        "aria-label": `${row.account_id}, rank ${row.fraud_rank}, ${whole.format(row.total_risk_points)} risk points, percentile ${Number(row.fraud_percentile).toFixed(2)}`
          + (row.burst_detected ? ", 30-day burst" : "") + ". Show its running balance.",
        onclick: () => choose(row.account_id) },
      el("span", { class: "rank-no", text: row.fraud_rank }),
      el("span", { class: "rank-id" }, el("span", { text: row.account_id }),
        row.account_status === "FLAGGED" ? statusPill("FLAGGED") : null,
        row.burst_detected ? el("span", { class: "burst" }, el("span", { "aria-hidden": "true", text: "▲" }), " 30-day burst") : null),
      el("span", { class: "rank-track" }, fill),
      el("span", { class: "rank-points", text: whole.format(row.total_risk_points) }),
      el("span", { class: "rank-pct", text: Number(row.fraud_percentile).toFixed(2) }));
      buttons.push(button);
      return el("li", null, button);
    }));
    card.append(el("div", { class: "rank-head", "aria-hidden": "true" },
      el("span", { text: "#" }), el("span", { text: "Account" }), el("span", { text: "Risk points" }), el("span"), el("span", { text: "Percentile" })),
    list);
    // Show the account chosen before, if it is still listed; otherwise the first burst, otherwise the top account.
    const chosen = items.find((row) => row.account_id === state.balanceAccount)
      || items.find((row) => row.burst_detected) || items[0];
    choose(chosen.account_id);
    return card;
  }

  async function loadBalance(host, accountId, period) {
    const existing = host.querySelector(".panel");
    if (existing) existing.classList.add("refreshing");
    else host.replaceChildren(panel("Running balance", null, el("p", { class: "notice", text: "Loading…" })));
    let data;
    try {
      data = await api("GET", `/accounts/${encodeURIComponent(accountId)}/balance`, { params: { from: period.from, to: period.to } });
    } catch (error) {
      if (state.balanceAccount !== accountId) return;
      if (error.status === 401) { handleFailure(error, $("page")); return; }
      host.replaceChildren(panel("Running balance", null, failureNotice(error)));
      return;
    }
    if (state.balanceAccount !== accountId || !host.isConnected) return;     // another account was chosen meanwhile

    const points = data.points;
    const summary = data.summary;
    const chart = el("div");
    const card = panel(`Running balance: ${data.account_id}`,
      "Worked out by the window function in monthly_summary.sql from the account's whole history, so a chosen period starts from the true balance. Select a point to see why it was scored.");
    if (data.account_status !== "ACTIVE") card.querySelector("h2").append(statusPill(data.account_status));
    if (!points.length) {
      card.append(el("p", { class: "notice", text: "No transactions on this account in this period." }));
      host.replaceChildren(card);
      return;
    }
    card.append(...[
      el("dl", { class: "inline-facts" },
        el("div", null, el("dt", { text: "Opening" }), el("dd", { text: formatMoney(summary.opening_balance) })),
        el("div", null, el("dt", { text: "Money in" }), el("dd", { text: formatMoney(summary.credits) })),
        el("div", null, el("dt", { text: "Money out" }), el("dd", { text: formatMoney(summary.debits) })),
        el("div", null, el("dt", { text: "Closing" }), el("dd", { text: formatMoney(summary.closing_balance) }))),
      FinTrustCharts.legend([
        { name: "Balance after each transaction", cls: "balance", kind: "line" },
        { name: `High-risk transaction (${whole.format(summary.high_risk_transactions)})`, cls: "high", kind: "dot" },
      ]),
      chart,
      data.truncated ? el("p", { class: "lede small", text: `Showing the latest ${whole.format(points.length)} transactions.` }) : null,
      numbers([
        { label: "Date", cell: (p) => formatDate(p.tx_date) },
        { label: "Transaction", cell: (p) => p.transaction_id },
        { label: "Type", cell: (p) => titleCase(p.tx_type) },
        { label: "Amount", number: true, cell: (p) => formatMoney(p.signed_amount) },
        { label: "Balance after", number: true, cell: (p) => formatMoney(p.running_balance) },
        { label: "Risk", cell: (p) => riskPill(p.risk_level) },
      ], points),
      el("p", { class: "panel-foot" }, el("a", { class: "link-button", href: `#/transactions?account_id=${encodeURIComponent(data.account_id)}` },
        `See all transactions on ${data.account_id}`))].filter(Boolean));
    host.replaceChildren(card);

    FinTrustCharts.stepChart(chart, {
      label: `Running balance of ${data.account_id}`,
      points: points.map((p) => ({
        t: new Date(String(p.tx_date).replace(" ", "T")).getTime(),
        v: Number(p.running_balance),
        highlight: p.risk_level === "HIGH",
        title: `${formatDate(p.tx_date)} · ${p.transaction_id}`,
        rows: [
          { value: formatMoney(p.running_balance), label: "balance after", key: "key-balance" },
          { value: (p.tx_type === "CREDIT" ? "+" : "−") + formatMoney(p.amount), label: titleCase(p.tx_type) },
          { value: `${p.risk_score}`, label: `${titleCase(p.risk_level)} risk`, key: "key-" + p.risk_level.toLowerCase() },
        ],
      })),
      // The line starts at the balance carried in from before the period, and runs to its end.
      opening: period.from ? { t: Date.parse(period.from + "T00:00:00+02:00"), v: Number(summary.opening_balance) } : null,
      until: period.to ? Math.min(Date.now(), Date.parse(period.to + "T23:59:59+02:00")) : null,
      tick: compactMoney,
      date: (t) => dayShort.format(new Date(t + 2 * 3600 * 1000)),      // South African calendar day
      onSelect: (i) => openJourney(points[i].transaction_id, points[i]),
    });
  }

  function statusPill(status) {
    const tone = { FLAGGED: "high", CLOSED: "plain", ACTIVE: "low", OPEN: "high", RESOLVED: "low" }[status] || "plain";
    return el("span", { class: "pill " + tone, text: status ? status.charAt(0) + status.slice(1).toLowerCase() : "" });
  }

  // ------------------------------------------------------------- transactions

  const TRANSACTION_SORTS = [
    ["newest", "Newest first"], ["oldest", "Oldest first"],
    ["amount_desc", "Amount: high to low"], ["amount_asc", "Amount: low to high"],
    ["account_asc", "Account: A to Z"], ["account_desc", "Account: Z to A"],
    ["merchant_asc", "Merchant: A to Z"], ["merchant_desc", "Merchant: Z to A"],
    ["score_desc", "Risk score: high to low"], ["score_asc", "Risk score: low to high"],
  ];
  const ANY = ["", "Any"];
  const LEVEL_OPTIONS = [ANY, ["HIGH", "High"], ["MEDIUM", "Medium"], ["LOW", "Low"]];
  const TYPE_OPTIONS = [ANY, ["CREDIT", "Credit (money in)"], ["DEBIT", "Debit (money out)"]];
  const CHANNEL_OPTIONS = [ANY, ...["ONLINE", "EFT", "POS", "MOBILE", "ATM", "BRANCH"].map((value) => [value, value === "POS" || value === "ATM" || value === "EFT" ? value : titleCase(value)])];

  /** Digits alone are short for an account id ("1234" means "ACC-1234"), as the API also accepts. */
  const accountShorthand = (value) => (/^\d+$/.test(value) ? "ACC-" + value : value.toUpperCase());

  function textInput(param, value, attributes) {
    return el("input", { type: "text", "data-param": param, "data-key": param, value: value || "", spellcheck: "false",
      autocomplete: "off", ...attributes });
  }

  /** A select that applies as soon as it changes. The whole form is applied, so
   *  anything typed in the other boxes is kept. */
  function liveSelect(param, options, value, apply) {
    const control = select(param, options, value);
    control.dataset.param = param;
    control.addEventListener("change", () => {
      if (control.form) control.form.requestSubmit();
      else apply({ [param]: control.value });
    });
    return control;
  }

  views.transactions = function (page, params) {
    return listView(page, params, {
      view: "transactions", title: "Transactions",
      lede: "Select a row to see the journey the transaction took and why it got its score.",
      defaults: { sort: "newest" }, sorts: TRANSACTION_SORTS,
      primary: (query, apply) => {
        const from = el("input", { type: "date", "data-param": "from", "data-key": "from", value: query.from || "" });
        const to = el("input", { type: "date", "data-param": "to", "data-key": "to", value: query.to || "" });
        return [
          field("Account", textInput("account_id", query.account_id, { placeholder: "ACC-1007", inputmode: "text" })),
          field("Risk level", liveSelect("risk_level", LEVEL_OPTIONS, query.risk_level, apply)),
          field("From", from), field("To", to),
        ];
      },
      advanced: (query, apply) => {
        const fields = [
          field("Type", liveSelect("tx_type", TYPE_OPTIONS, query.tx_type, apply)),
          field("Channel", liveSelect("channel", CHANNEL_OPTIONS, query.channel, apply)),
          field("Country", textInput("country_code", query.country_code, { placeholder: "GB", maxlength: "2" }), "Two letters"),
          field("Smallest amount (R)", textInput("min_amount", query.min_amount, { inputmode: "decimal", placeholder: "50000" })),
          field("Largest amount (R)", textInput("max_amount", query.max_amount, { inputmode: "decimal" })),
          field("Merchant contains", textInput("merchant", query.merchant, { maxlength: "60", placeholder: "Karoo" })),
        ];
        const active = ["tx_type", "channel", "country_code", "min_amount", "max_amount", "merchant"].filter((key) => query[key]).length;
        return { fields, active };
      },
      check: (values) => {
        if (values.from && values.to && values.from > values.to) return "The From date must not be after the To date.";
        const amount = /^\d+([.,]\d{1,2})?$/;
        for (const key of ["min_amount", "max_amount"]) {
          values[key] = (values[key] || "").replace(/[\sR]/g, "").replace(",", ".");
          if (values[key] && !amount.test(values[key])) return "Amounts are numbers in rand, for example 50000 or 1250.50.";
        }
        if (values.min_amount && values.max_amount && Number(values.min_amount) > Number(values.max_amount)) {
          return "The smallest amount must not be more than the largest.";
        }
        if (values.account_id) values.account_id = accountShorthand(values.account_id);
        if (values.country_code) values.country_code = values.country_code.toUpperCase();
        return null;
      },
      chips: (query) => [
        query.account_id && { label: `Account ${query.account_id}`, keys: ["account_id"] },
        query.risk_level && { label: `${titleCase(query.risk_level)} risk`, keys: ["risk_level"] },
        (query.from || query.to) && { label: dateRangeLabel(query.from, query.to), keys: ["from", "to"] },
        query.tx_type && { label: query.tx_type === "CREDIT" ? "Money in" : "Money out", keys: ["tx_type"] },
        query.channel && { label: `Channel ${query.channel}`, keys: ["channel"] },
        query.country_code && { label: `Country ${query.country_code}`, keys: ["country_code"] },
        (query.min_amount || query.max_amount) && { label: amountRangeLabel(query.min_amount, query.max_amount), keys: ["min_amount", "max_amount"] },
        query.merchant && { label: `Merchant “${query.merchant}”`, keys: ["merchant"] },
      ],
      load: (query, results, apply) => {
        api("GET", "/transactions", { params: { ...query, limit: PAGE_SIZE } }).then((data) => {
          results.classList.remove("refreshing");
          const sorting = sortingFor(query, apply, TRANSACTION_SORTS);
          keepFocus(() => results.replaceChildren(
            resultsSummary(data.pagination.total, "transaction", TRANSACTION_SORTS, data.sort),
            table([
              { label: "Date", sort: { desc: "newest", asc: "oldest" }, cell: (r) => formatDate(r.tx_date) },
              { label: "Transaction", cell: (r) => r.transaction_id },
              { label: "Account", sort: { asc: "account_asc", desc: "account_desc", first: "asc" }, cell: (r) => r.account_id },
              { label: "Type", cell: (r) => r.tx_type === "CREDIT" ? "Credit" : "Debit" },
              { label: "Amount", number: true, sort: { desc: "amount_desc", asc: "amount_asc" }, cell: (r) => formatMoney(r.amount) },
              { label: "Country", cell: (r) => r.country_code },
              { label: "Merchant", sort: { asc: "merchant_asc", desc: "merchant_desc", first: "asc" }, cell: (r) => r.merchant_name || "" },
              { label: "Score", number: true, sort: { desc: "score_desc", asc: "score_asc" }, cell: (r) => r.risk_score },
              { label: "Risk", cell: (r) => riskPill(r.risk_level) },
            ], data.items, "No transactions match these filters.", (row) => openJourney(row.transaction_id, row), sorting),
            pager(data.pagination, (offset) => apply({ offset }, true))));
        }).catch((error) => { results.classList.remove("refreshing"); handleFailure(error, results); });
      },
    });
  };

  function dateRangeLabel(from, to) {
    const day = (text) => dayShort.format(asDay(text));
    if (from && to) return `${day(from)} to ${day(to)}`;
    return from ? `From ${day(from)}` : `Up to ${day(to)}`;
  }

  function amountRangeLabel(minimum, maximum) {
    if (minimum && maximum) return `${amountText(minimum)} to ${amountText(maximum)}`;
    return minimum ? `${amountText(minimum)} or more` : `Up to ${amountText(maximum)}`;
  }

  // ------------------------------------------------------------------- alerts

  const ALERT_SORTS = [
    ["newest", "Newest first"], ["oldest", "Oldest first"],
    ["amount_desc", "Amount: high to low"], ["amount_asc", "Amount: low to high"],
    ["score_desc", "Risk score: high to low"],
    ["account_asc", "Account: A to Z"], ["account_desc", "Account: Z to A"],
  ];

  views.alerts = function (page, params) {
    const canResolve = state.session.permissions.resolve_alerts;
    return listView(page, params, {
      view: "alerts", title: "Alerts",
      lede: canResolve
        ? "One alert is raised for every transaction scored High. Resolve an alert once it has been checked; it is recorded under your name."
        : "One alert is raised for every transaction scored High. Your role can review alerts; resolving them needs the Compliance officer role.",
      defaults: { sort: "newest", status: "OPEN" }, sorts: ALERT_SORTS, cleared: { status: "all" },
      primary: (query, apply) => [
        field("Show", liveSelect("status", [["OPEN", "Open"], ["RESOLVED", "Resolved"], ["all", "All"]], query.status, apply)),
        field("Account", textInput("account_id", query.account_id, { placeholder: "ACC-1007" })),
        field("Raised from", el("input", { type: "date", "data-param": "from", "data-key": "from", value: query.from || "" })),
        field("Raised to", el("input", { type: "date", "data-param": "to", "data-key": "to", value: query.to || "" })),
      ],
      check: (values) => {
        if (values.from && values.to && values.from > values.to) return "The From date must not be after the To date.";
        if (values.account_id) values.account_id = accountShorthand(values.account_id);
        return null;
      },
      chips: (query) => [
        query.status !== "all" && { label: query.status === "RESOLVED" ? "Resolved" : "Open", keys: ["status"] },
        query.account_id && { label: `Account ${query.account_id}`, keys: ["account_id"] },
        (query.from || query.to) && { label: "Raised " + dateRangeLabel(query.from, query.to).toLowerCase(), keys: ["from", "to"] },
      ],
      load: (query, results, apply) => {
        const status = query.status === "all" ? "" : query.status;
        api("GET", "/alerts", { params: { ...query, status, limit: PAGE_SIZE } }).then((data) => {
          results.classList.remove("refreshing");
          const columns = [
            { label: "Raised", sort: { desc: "newest", asc: "oldest" }, cell: (r) => formatDate(r.created_at) },
            { label: "Transaction", cell: (r) => r.transaction_id },
            { label: "Account", sort: { asc: "account_asc", desc: "account_desc", first: "asc" }, cell: (r) => r.account_id },
            { label: "Amount", number: true, sort: { desc: "amount_desc", asc: "amount_asc" }, cell: (r) => formatMoney(r.amount) },
            { label: "Country", cell: (r) => r.country_code },
            { label: "Score", number: true, sort: { desc: "score_desc" }, cell: (r) => r.risk_score },
            { label: "Status", cell: (r) => statusPill(r.status) },
            { label: "Resolved by", wrap: true, cell: (r) => r.resolved_by
              ? `${r.resolved_by}, ${formatDate(r.resolved_at)}${r.resolution_note ? ": " + r.resolution_note : ""}` : "" },
          ];
          if (canResolve) {
            columns.push({ label: "", cell: (r) => r.status === "OPEN"
              ? el("button", { type: "button", class: "button small", onclick: () => resolveAlert(r, query) }, "Resolve") : "" });
          }
          keepFocus(() => results.replaceChildren(
            resultsSummary(data.pagination.total, status === "OPEN" ? "open alert" : status === "RESOLVED" ? "resolved alert" : "alert", ALERT_SORTS, data.sort),
            table(columns, data.items, status === "OPEN" && !query.account_id ? "No open alerts. Nothing is waiting for review." : "No alerts match these filters.",
              (row) => openJourney(row.transaction_id, row), sortingFor(query, apply, ALERT_SORTS)),
            pager(data.pagination, (offset) => apply({ offset }, true))));
        }).catch((error) => { results.classList.remove("refreshing"); handleFailure(error, results); });
      },
    });
  };

  function resolveAlert(alert, query) {
    const note = el("textarea", { maxlength: "500" });
    const session = state.session;
    openDialog({
      title: "Resolve alert",
      text: `Transaction ${alert.transaction_id} on ${alert.account_id}, ${formatMoney(alert.amount)}.`,
      fields: [
        el("p", { class: "signed-as" }, "Recorded as ", el("strong", { text: session.name }), ` (${session.username})`),
        field("What was checked (optional)", note),
      ],
      confirm: "Resolve alert",
      action: async () => {
        await api("POST", "/resolve", { body: { alert_id: alert.alert_id, resolution_note: note.value.trim() || undefined } });
        toast("Alert resolved");
        state.update(query);
        refreshAlertCount();
      },
    });
  }

  // ----------------------------------------------------------------- accounts

  const ACCOUNT_SORTS = [
    ["risk_desc", "Risk points: high to low"], ["risk_asc", "Risk points: low to high"],
    ["account_asc", "Account: A to Z"], ["account_desc", "Account: Z to A"],
    ["recent", "Most recently active"], ["transactions_desc", "Most transactions"],
    ["high_risk_desc", "Most high-risk transactions"], ["open_alerts_desc", "Most open alerts"],
  ];

  views.accounts = function (page, params) {
    const canFlag = state.session.permissions.flag_accounts;
    return listView(page, params, {
      view: "accounts", title: "Accounts",
      lede: "Total risk points are the sum of the risk scores of an account's transactions. Select an account to see its transactions."
        + (canFlag ? "" : " Flagging an account needs the Compliance officer role."),
      defaults: { sort: "risk_desc" }, sorts: ACCOUNT_SORTS,
      primary: (query, apply) => [
        field("Account contains", textInput("search", query.search, { placeholder: "1007", maxlength: "20" })),
        field("Status", liveSelect("status", [ANY, ["ACTIVE", "Active"], ["FLAGGED", "Flagged"], ["CLOSED", "Closed"]], query.status, apply)),
        field("Type", liveSelect("account_type", [ANY, ["CHEQUE", "Cheque"], ["SAVINGS", "Savings"], ["CREDIT", "Credit"], ["BUSINESS", "Business"]],
          query.account_type, apply)),
      ],
      chips: (query) => [
        query.search && { label: `Account contains “${query.search}”`, keys: ["search"] },
        query.status && { label: titleCase(query.status), keys: ["status"] },
        query.account_type && { label: titleCase(query.account_type), keys: ["account_type"] },
      ],
      load: (query, results, apply) => {
        api("GET", "/accounts", { params: { ...query, limit: PAGE_SIZE } }).then((data) => {
          results.classList.remove("refreshing");
          const columns = [
            { label: "Account", sort: { asc: "account_asc", desc: "account_desc", first: "asc" }, cell: (r) => r.account_id },
            { label: "Type", cell: (r) => titleCase(r.account_type) },
            { label: "Status", cell: (r) => statusPill(r.account_status) },
            { label: "Transactions", number: true, sort: { desc: "transactions_desc" }, cell: (r) => whole.format(r.transaction_count) },
            { label: "High risk", number: true, sort: { desc: "high_risk_desc" }, cell: (r) => whole.format(r.high_risk_count) },
            { label: "Risk points", number: true, sort: { desc: "risk_desc", asc: "risk_asc" }, cell: (r) => whole.format(r.total_risk_points) },
            { label: "Open alerts", number: true, sort: { desc: "open_alerts_desc" }, cell: (r) => whole.format(r.open_alerts) },
            { label: "Manual flags", number: true, cell: (r) => whole.format(r.manual_flags) },
            { label: "Last transaction", sort: { desc: "recent" }, cell: (r) => formatDate(r.last_transaction_at) },
          ];
          if (canFlag) {
            columns.push({ label: "", cell: (r) => r.account_status === "CLOSED" ? ""
              : el("button", { type: "button", class: "button small", onclick: () => flagAccount(r, query) }, "Flag") });
          }
          keepFocus(() => results.replaceChildren(
            resultsSummary(data.pagination.total, "account", ACCOUNT_SORTS, data.sort),
            table(columns, data.items, "No accounts match these filters.",
              (row) => navigate("transactions", { account_id: row.account_id }), sortingFor(query, apply, ACCOUNT_SORTS)),
            pager(data.pagination, (offset) => apply({ offset }, true))));
        }).catch((error) => { results.classList.remove("refreshing"); handleFailure(error, results); });
      },
    });
  };

  function flagAccount(account, query) {
    const reason = el("textarea", { required: true, maxlength: "500" });
    const session = state.session;
    openDialog({
      title: "Flag account " + account.account_id,
      text: "The reason is recorded with the flag and cannot be edited afterwards.",
      fields: [
        el("p", { class: "signed-as" }, "Recorded as ", el("strong", { text: session.name }), ` (${session.username})`),
        field("Reason", reason),
      ],
      confirm: "Flag account",
      action: async () => {
        await api("POST", "/flag", { body: { account_id: account.account_id, reason: reason.value.trim() } });
        toast("Account flagged");
        state.update(query);
      },
    });
  }

  // ------------------------------------------------------------------- upload

  views.upload = function (page) {
    const chooser = el("input", { type: "file", accept: ".csv,text/csv", "aria-label": "CSV file" });
    const button = el("button", { type: "button", class: "button primary" }, "Upload file");
    const result = el("div", { class: "section" });

    button.addEventListener("click", async () => {
      const file = chooser.files[0];
      if (!file) { result.replaceChildren(el("p", { class: "notice error", role: "alert", text: "Choose a CSV file first." })); return; }
      if (file.size > MAX_UPLOAD_BYTES) {
        result.replaceChildren(el("p", { class: "notice error", role: "alert", text: "The file is larger than 5 MB. Split it into smaller files." }));
        return;
      }
      stopTracking();
      button.disabled = true;
      button.textContent = "Uploading…";
      try {
        const text = await file.text();
        const safeName = file.name.replace(/[^A-Za-z0-9._-]/g, "_").replace(/^[^A-Za-z0-9]+/, "").slice(0, 80) || "upload.csv";
        const accepted = await api("POST", "/upload", { params: { filename: safeName }, csv: text });
        chooser.value = "";
        showTracking(result, accepted, transactionIds(text));
      } catch (error) {
        handleFailure(error, result);
      } finally {
        button.disabled = false;
        button.textContent = "Upload file";
      }
    });

    page.replaceChildren(
      pageHead("Upload a file", "Send a CSV of transactions. The file is accepted at once and processed in the background: each row is checked, scored and stored, and High risk rows raise an alert."),
      el("div", { class: "dropzone" }, chooser, button),
      el("p", { class: "lede section", text: "The first line must name the columns: transaction_id, account_id, tx_date, amount, tx_type, channel and country_code are required. Rows that fail a check are set aside with the reason and are not stored. The file is recorded as uploaded by you." }),
      result);
    return null;
  };

  /** The transaction ids in a CSV, read from whichever column is named transaction_id. */
  function transactionIds(text) {
    const lines = text.replace(/^﻿/, "").split(/\r?\n/).filter((line) => line.trim());
    if (lines.length < 2) return [];
    const column = lines[0].split(",").map((name) => name.trim().toLowerCase()).indexOf("transaction_id");
    if (column < 0) return [];
    const seen = new Set();
    for (const line of lines.slice(1)) {
      const id = (line.split(",")[column] || "").trim();
      if (/^[A-Za-z0-9][A-Za-z0-9-]{5,39}$/.test(id)) seen.add(id);
    }
    return [...seen];
  }

  function showTracking(container, accepted, ids) {
    const followed = ids.slice(0, TRACK_LIMIT);
    const statuses = new Map(followed.map((id) => [id, null]));
    const body = el("div");
    const note = el("p", { class: "lede" });
    container.replaceChildren(
      el("p", { class: "notice good", role: "status",
        text: `File accepted and stored as ${accepted.object_key}. Processing has started.` }),
      el("div", { class: "section" }, el("h2", { text: "Progress" }), note, body));

    const draw = () => {
      const done = [...statuses.values()].filter((s) => s && FINISHED.includes(s.status)).length;
      note.textContent = followed.length
        ? `Following ${followed.length} of ${ids.length} transactions in the file. ${done} finished.`
        : "No transaction ids could be read from the file, so progress cannot be shown here.";
      body.replaceChildren(followed.length ? table([
        { label: "Transaction", cell: (id) => id },
        { label: "Where it is now", cell: (id) => {
          const journey = statuses.get(id);
          if (!journey) return el("span", { class: "pill plain", text: "No record yet" });
          const tone = { ALERTED: "high", DEAD_LETTERED: "high", RETRYING: "medium", SCORED: "low" }[journey.status] || "plain";
          return el("span", { class: "pill " + tone, text: STATUS_NAMES[journey.status] || journey.status });
        } },
        { label: "Risk", cell: (id) => statuses.get(id) && statuses.get(id).score ? riskPill(statuses.get(id).score.risk_level) : "" },
        { label: "Score", number: true, cell: (id) => statuses.get(id) && statuses.get(id).score ? statuses.get(id).score.risk_score : "" },
      ], followed, "", (id) => { if (statuses.get(id)) openJourney(id); }) : "");
    };
    draw();
    if (!followed.length) return;

    const started = Date.now();
    const tracker = { stopped: false };
    state.tracker = tracker;
    const tick = async () => {
      for (const id of followed) {
        if (tracker.stopped) return;
        const current = statuses.get(id);
        if (current && FINISHED.includes(current.status)) continue;
        try {
          statuses.set(id, await api("GET", `/transactions/${encodeURIComponent(id)}/journey`));
        } catch (error) {
          if (error.status === 401) { handleFailure(error, container); return; }
          // 404: nothing recorded yet, or the row failed validation and was set aside.
        }
      }
      if (tracker.stopped) return;
      draw();
      const allDone = [...statuses.values()].every((s) => s && FINISHED.includes(s.status));
      if (allDone) { refreshAlertCount(); return; }
      if (Date.now() - started > TRACK_FOR_MS) {
        note.textContent += " Rows still showing “No record yet” failed a validation check and were set aside in the quarantine bucket.";
        refreshAlertCount();
        return;
      }
      tracker.timer = setTimeout(tick, TRACK_EVERY_MS);
    };
    tracker.timer = setTimeout(tick, 1500);
  }

  function stopTracking() {
    if (state.tracker) {
      state.tracker.stopped = true;
      clearTimeout(state.tracker.timer);
      state.tracker = null;
    }
  }

  // ------------------------------------------------------------------ journey

  let returnFocusTo = null;

  async function openJourney(transactionId, row) {
    returnFocusTo = document.activeElement;
    const body = $("drawer-body");
    body.replaceChildren(el("p", { class: "notice", text: "Loading…" }));
    $("drawer").hidden = false;
    $("scrim").hidden = false;
    $("drawer-close").focus();

    try {
      const journey = await api("GET", `/transactions/${encodeURIComponent(transactionId)}/journey`);
      const facts = el("dl", { class: "facts" },
        el("dt", { text: "Transaction" }), el("dd", { text: journey.transaction_id }),
        el("dt", { text: "Account" }), el("dd", { text: journey.account_id }),
        row && row.amount !== undefined ? [el("dt", { text: "Amount" }), el("dd", { text: formatMoney(row.amount) })] : null,
        el("dt", { text: "Now" }), el("dd", { text: STATUS_NAMES[journey.status] || journey.status }));

      const parts = [facts];
      if (journey.score) {
        parts.push(el("h3", { text: "Why this score" }),
          el("div", { class: "score-line" }, el("span", { class: "score-number", text: journey.score.risk_score }),
            el("span", { text: "of 100" }), riskPill(journey.score.risk_level)),
          journey.score.rules.length
            ? el("ul", { class: "rules" }, journey.score.rules.map((rule) => el("li", null,
              el("span", { class: "points", text: "+" + rule.points }),
              el("div", null, el("div", { class: "rule-name", text: RULE_NAMES[rule.rule] || rule.rule }),
                el("div", { class: "rule-detail", text: rule.detail })))))
            : el("p", { class: "lede", text: "No risk rule fired for this transaction." }));
      }
      parts.push(el("h3", { text: "Journey" }),
        journey.events.length
          ? el("ol", { class: "rail" }, journey.events.map((event) => el("li",
            { class: event.stage.endsWith("FAILED") ? "failed" : event.stage === "ALERT_SENT" ? "alerted" : "done" },
            el("div", { class: "stage", text: STAGE_NAMES[event.stage] || event.stage }),
            el("div", { class: "when", text: formatDate(event.at, timeOnly) + ", " + formatDate(event.at).split(",")[0] }),
            event.detail ? el("div", { class: "detail", text: event.detail }) : null)))
          : el("p", { class: "lede", text: "This transaction was loaded as opening data, so it did not pass through the pipeline. Its score and rules are read from the ledger." }));
      body.replaceChildren(...parts.flat().filter(Boolean));
    } catch (error) {
      if (error.status === 404) {
        body.replaceChildren(el("p", { class: "notice", text: "Nothing is recorded for this transaction." }));
      } else if (error.status === 401) {
        closeDrawer();
        handleFailure(error, $("page"));
      } else {
        body.replaceChildren(failureNotice(error));
      }
    }
  }

  function closeDrawer() {
    $("drawer").hidden = true;
    $("scrim").hidden = true;
    if (returnFocusTo && document.contains(returnFocusTo)) returnFocusTo.focus();
    returnFocusTo = null;
  }

  // ------------------------------------------------------------------- dialog

  let dialogAction = null;

  function openDialog({ title, text, fields, confirm, action }) {
    $("dialog-title").textContent = title;
    $("dialog-text").textContent = text;
    $("dialog-fields").replaceChildren(...fields);
    $("dialog-confirm").textContent = confirm;
    $("dialog-confirm").dataset.label = confirm;
    $("dialog-error").hidden = true;
    dialogAction = action;
    $("dialog").showModal();
  }

  async function submitDialog(event) {
    event.preventDefault();
    const button = $("dialog-confirm");
    button.disabled = true;
    button.textContent = "Saving…";
    try {
      await dialogAction();
      $("dialog").close();
    } catch (error) {
      if (error.status === 401) { $("dialog").close(); handleFailure(error, $("page")); return; }
      $("dialog-error").textContent = error.message;
      $("dialog-error").hidden = false;
    } finally {
      button.disabled = false;
      button.textContent = button.dataset.label;
    }
  }

  // -------------------------------------------------------------------- start

  $("sign-in").addEventListener("click", beginSignIn);
  $("sign-out").addEventListener("click", () => { closeUserMenu(); endSession("SIGNED_OUT"); });
  $("theme-toggle").addEventListener("click", () => FinTrustTheme.toggle());
  FinTrustTheme.onChange(() => {
    applyThemeButton();
    // Charts read their colours from CSS, so a redraw is enough to follow the new appearance.
    for (const chart of document.querySelectorAll(".chart")) if (chart._draw) chart._draw();
  });
  for (const input of document.querySelectorAll("#appearance input")) {
    input.addEventListener("change", () => FinTrustTheme.set(input.value));
  }
  $("user-button").addEventListener("click", () => ($("user-menu").hidden ? openUserMenu() : closeUserMenu(true)));
  document.addEventListener("click", (event) => {
    if (!$("user-menu").hidden && !event.target.closest("#user")) closeUserMenu(false);
  });
  $("drawer-close").addEventListener("click", closeDrawer);
  $("scrim").addEventListener("click", closeDrawer);
  $("dialog-form").addEventListener("submit", submitDialog);
  $("dialog-cancel").addEventListener("click", () => $("dialog").close());
  $("idle-stay").addEventListener("click", stayActive);
  $("idle-sign-out").addEventListener("click", () => endSession("SIGNED_OUT"));
  $("idle-dialog").addEventListener("cancel", (event) => { event.preventDefault(); stayActive(); });   // Escape means "I'm here"
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!$("drawer").hidden) closeDrawer();
    else if (!$("user-menu").hidden) closeUserMenu(true);
  });
  // Reading a chart with the mouse counts as being here, as does typing, scrolling and touching.
  for (const name of ["pointerdown", "pointermove", "keydown", "wheel", "touchstart"]) {
    document.addEventListener(name, noteActivity, { capture: true, passive: true });
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.session) checkIdle(); });
  window.addEventListener("hashchange", render);

  start();
})();
