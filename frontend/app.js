/* FinTrust risk desk: plain JavaScript, no libraries, no build step.
 *
 * Every number on screen comes from the real API (API Gateway -> query Lambda).
 * Nothing is mocked.
 *
 * Two safety rules are followed everywhere in this file:
 *   1. The API key lives only in sessionStorage (this tab, until it closes). It
 *      is never written into a file, a URL or the page's HTML.
 *   2. Text from the API is always put on the page with textContent, never as
 *      HTML, so a value such as a merchant name can never run as script.
 */
"use strict";

(function () {
  const STORAGE_URL = "fintrust.apiUrl";
  const STORAGE_KEY = "fintrust.apiKey";
  const PAGE_SIZE = 25;
  const TRACK_LIMIT = 12;            // transactions followed live after an upload
  const TRACK_EVERY_MS = 4000;       // gentle on the API's rate limit (10 requests a second)
  const TRACK_FOR_MS = 4 * 60 * 1000;
  const MAX_UPLOAD_BYTES = 5 * 1024 * 1024;
  const FINISHED = ["SCORED", "ALERTED", "DEAD_LETTERED"];

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

  const state = { apiUrl: "", apiKey: "", view: "overview", tracker: null };
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

  const money = new Intl.NumberFormat("en-ZA", { style: "currency", currency: "ZAR" });
  const whole = new Intl.NumberFormat("en-ZA");
  const dateTime = new Intl.DateTimeFormat("en-ZA", {
    timeZone: "Africa/Johannesburg", year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
  const timeOnly = new Intl.DateTimeFormat("en-ZA", {
    timeZone: "Africa/Johannesburg", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });

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

  // ---------------------------------------------------------------------- API

  class ApiFailure extends Error {
    constructor(status, message, requestId) {
      super(message);
      this.status = status;
      this.requestId = requestId;
    }
  }

  /** Call the API. Returns the parsed JSON body, or throws ApiFailure with a message fit to show. */
  async function api(method, path, options) {
    const { params, body, csv } = options || {};
    const url = new URL(state.apiUrl.replace(/\/+$/, "") + path);
    for (const [name, value] of Object.entries(params || {})) {
      if (value !== "" && value !== null && value !== undefined) url.searchParams.set(name, value);
    }
    const headers = { "x-api-key": state.apiKey };
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
      throw new ApiFailure(0, "The API could not be reached. Check the address and your connection.");
    }

    let data = null;
    try { data = await response.json(); } catch (_error) { /* an empty or non-JSON body */ }

    if (response.ok) return data;
    if (response.status === 403) throw new ApiFailure(403, "The API key was not accepted.");
    if (response.status === 429) throw new ApiFailure(429, "Too many requests. Wait a moment and try again.");
    const error = (data && data.error) || {};
    throw new ApiFailure(response.status, error.message || (data && data.message) || `The API answered ${response.status}.`,
      error.request_id);
  }

  function failureNotice(error) {
    const reference = error.requestId ? ` Reference: ${error.requestId}` : "";
    return el("p", { class: "notice error", role: "alert", text: error.message + reference });
  }

  // ------------------------------------------------------------------ connect

  function showConnect(message) {
    stopTracking();
    closeDrawer();
    $("app-view").hidden = true;
    $("session").hidden = true;
    $("connect-view").hidden = false;
    $("api-url").value = state.apiUrl || (window.FINTRUST_CONFIG && window.FINTRUST_CONFIG.apiUrl) || "";
    $("api-key").value = "";
    $("connect-error").hidden = !message;
    $("connect-error").textContent = message || "";
    ($("api-url").value ? $("api-key") : $("api-url")).focus();
  }

  async function connect(event) {
    event.preventDefault();
    const button = $("connect-button");
    state.apiUrl = $("api-url").value.trim();
    state.apiKey = $("api-key").value.trim();
    if (!/^https:\/\//.test(state.apiUrl) && !/^http:\/\/(localhost|127\.0\.0\.1)(:|\/)/.test(state.apiUrl)) {
      showConnectError("The API address must start with https://");
      return;
    }
    button.disabled = true;
    button.textContent = "Connecting…";
    try {
      const health = await api("GET", "/health");
      if (!health || health.status !== "ok") throw new ApiFailure(503, "The API is up but cannot reach its database.");
      sessionStorage.setItem(STORAGE_URL, state.apiUrl);
      sessionStorage.setItem(STORAGE_KEY, state.apiKey);
      showApp();
    } catch (error) {
      state.apiKey = "";
      showConnectError(error.status === 503
        ? "The API is up but its database is not available. Start the database, then try again."
        : error.message);
    } finally {
      button.disabled = false;
      button.textContent = "Connect";
    }
  }

  function showConnectError(message) {
    $("connect-error").textContent = message;
    $("connect-error").hidden = false;
  }

  function disconnect() {
    sessionStorage.removeItem(STORAGE_KEY);
    state.apiKey = "";
    showConnect();
  }

  function showApp() {
    $("connect-view").hidden = true;
    $("app-view").hidden = false;
    $("session").hidden = false;
    $("session-text").textContent = "Connected to " + new URL(state.apiUrl).host;
    go(state.view);
    refreshAlertCount();
  }

  /** A 403 in the middle of a session means the key was revoked or rotated. */
  function handleFailure(error, container) {
    if (error.status === 403) {
      sessionStorage.removeItem(STORAGE_KEY);
      state.apiKey = "";
      showConnect("The API key is no longer accepted. Enter the current key.");
      return;
    }
    container.replaceChildren(failureNotice(error));
  }

  // --------------------------------------------------------------- navigation

  const views = {};

  function go(view) {
    state.view = view;
    stopTracking();
    for (const tab of document.querySelectorAll(".tab")) {
      tab.setAttribute("aria-selected", String(tab.dataset.view === view));
    }
    const page = $("page");
    page.replaceChildren(el("p", { class: "notice", text: "Loading…" }));
    views[view](page);
  }

  async function refreshAlertCount() {
    try {
      const data = await api("GET", "/alerts", { params: { status: "OPEN", limit: 1 } });
      const badge = $("alert-count");
      badge.textContent = whole.format(data.pagination.total);
      badge.hidden = data.pagination.total === 0;
    } catch (_error) { /* the badge is a convenience; the Alerts view reports real errors */ }
  }

  function pageHead(title, lede, ...extra) {
    return el("div", { class: "page-head" },
      el("div", null, el("h1", { text: title }), lede && el("p", { class: "lede", text: lede })), ...extra);
  }

  function table(columns, rows, emptyText, onRowClick) {
    if (!rows.length) return el("div", { class: "table-wrap" }, el("p", { class: "empty", text: emptyText }));
    const head = el("tr", null, columns.map((c) => el("th", { class: c.number ? "number" : null, scope: "col", text: c.label })));
    const body = rows.map((row) => {
      const cells = columns.map((c) => el("td", { class: [c.number ? "number" : "", c.wrap ? "wrap" : ""].join(" ").trim() || null }, c.cell(row)));
      const line = el("tr", onRowClick ? { class: "clickable", tabindex: "0" } : null, cells);
      if (onRowClick) {
        line.addEventListener("click", (event) => { if (!event.target.closest("button")) onRowClick(row); });
        line.addEventListener("keydown", (event) => { if (event.key === "Enter") onRowClick(row); });
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
        el("button", { type: "button", class: "button small", disabled: pagination.offset === 0,
          onclick: () => onMove(Math.max(0, pagination.offset - pagination.limit)) }, "Previous"),
        el("button", { type: "button", class: "button small", disabled: !pagination.has_more,
          onclick: () => onMove(pagination.offset + pagination.limit) }, "Next")));
  }

  function field(label, input) {
    const id = "f-" + Math.random().toString(36).slice(2, 9);
    input.id = id;
    return el("div", { class: "field" }, el("label", { for: id, text: label }), input);
  }

  // ----------------------------------------------------------------- overview

  views.overview = function (page, period) {
    const from = el("input", { type: "date", value: (period && period.from) || "" });
    const to = el("input", { type: "date", value: (period && period.to) || "" });
    const filters = el("form", { class: "filters", onsubmit: (event) => {
      event.preventDefault();
      views.overview(page, { from: from.value, to: to.value });
    } }, field("From", from), field("To", to), el("button", { type: "submit", class: "button" }, "Show period"));

    const content = el("div", null, el("p", { class: "notice", text: "Loading…" }));
    page.replaceChildren(
      pageHead("Overview", "What has been processed and what needs attention. Leave the dates empty to see everything."),
      filters, content);

    api("GET", "/insights", { params: { from: from.value, to: to.value } }).then((data) => {
      const levels = data.by_risk_level;
      const total = data.totals.transactions;
      const share = el("div", { class: "share", role: "img",
        "aria-label": `Low ${levels.LOW}, medium ${levels.MEDIUM}, high ${levels.HIGH}` });
      for (const level of ["LOW", "MEDIUM", "HIGH"]) {
        if (!levels[level]) continue;
        const part = el("span", { class: level.toLowerCase(), title: `${level}: ${levels[level]}` });
        part.style.flexGrow = String(levels[level]);
        share.append(part);
      }
      const key = el("ul", { class: "key" }, ["LOW", "MEDIUM", "HIGH"].map((level) => el("li", null,
        el("span", { class: "swatch " + level.toLowerCase(), "aria-hidden": "true" }),
        `${level.charAt(0) + level.slice(1).toLowerCase()} ${whole.format(levels[level])}`)));

      const mostFired = Math.max(1, ...Object.values(data.rules_fired));
      const rules = el("ul", { class: "bars" }, Object.entries(data.rules_fired).map(([rule, fired]) => {
        const fill = el("div", { class: "bar-fill" });
        fill.style.width = (100 * fired / mostFired) + "%";
        if (!fired) fill.style.minWidth = "0";
        return el("li", null, el("span", { text: RULE_NAMES[rule] || rule }),
          el("div", { class: "bar-track" }, fill), el("span", { class: "value", text: whole.format(fired) }));
      }));

      content.replaceChildren(
        el("div", { class: "figures" },
          figure(whole.format(total), "Transactions"),
          figure(formatMoney(data.totals.total_value), "Total value"),
          figure(whole.format(data.totals.accounts), "Active accounts"),
          figure(whole.format(levels.HIGH), "High risk", levels.HIGH > 0),
          figure(whole.format(data.open_alerts), "Open alerts", data.open_alerts > 0)),
        el("div", { class: "split section" },
          el("div", null, el("h2", { text: "Risk levels" }),
            total ? share : el("p", { class: "notice", text: "No transactions in this period." }), total ? key : null),
          el("div", null, el("h2", { text: "Rules that fired" }), rules)),
        el("div", { class: "section" }, el("h2", { text: "Accounts with the most high-risk transactions" }),
          table([
            { label: "Account", cell: (r) => r.account_id },
            { label: "Status", cell: (r) => statusPill(r.status) },
            { label: "High-risk transactions", number: true, cell: (r) => whole.format(r.high_risk_transactions) },
          ], data.top_risk_accounts, "No high-risk transactions in this period.",
          (row) => { go("transactions"); views.transactions($("page"), { account_id: row.account_id, risk_level: "HIGH" }); })));
    }).catch((error) => handleFailure(error, content));
  };

  function figure(value, label, attention) {
    return el("div", { class: "figure" + (attention ? " attention" : "") },
      el("div", { class: "figure-value", text: value }), el("div", { class: "figure-label", text: label }));
  }

  function statusPill(status) {
    const tone = { FLAGGED: "high", CLOSED: "plain", ACTIVE: "low", OPEN: "high", RESOLVED: "low" }[status] || "plain";
    return el("span", { class: "pill " + tone, text: status ? status.charAt(0) + status.slice(1).toLowerCase() : "" });
  }

  // ------------------------------------------------------------- transactions

  views.transactions = function (page, filter) {
    const query = Object.assign({ account_id: "", risk_level: "", from: "", to: "", offset: 0 }, filter);
    const account = el("input", { type: "text", value: query.account_id, placeholder: "ACC-1007", spellcheck: "false" });
    const level = el("select", null, [["", "Any"], ["HIGH", "High"], ["MEDIUM", "Medium"], ["LOW", "Low"]]
      .map(([value, label]) => el("option", { value, selected: value === query.risk_level }, label)));
    const from = el("input", { type: "date", value: query.from });
    const to = el("input", { type: "date", value: query.to });
    const results = el("div", null, el("p", { class: "notice", text: "Loading…" }));

    const search = (offset) => views.transactions(page, {
      account_id: account.value.trim(), risk_level: level.value, from: from.value, to: to.value, offset: offset || 0 });
    const filters = el("form", { class: "filters", onsubmit: (event) => { event.preventDefault(); search(0); } },
      field("Account", account), field("Risk level", level), field("From", from), field("To", to),
      el("button", { type: "submit", class: "button primary" }, "Search"),
      el("button", { type: "button", class: "button", onclick: () => views.transactions(page) }, "Clear"));

    page.replaceChildren(pageHead("Transactions", "Select a row to see the journey the transaction took and why it got its score."),
      filters, results);

    api("GET", "/transactions", { params: {
      account_id: query.account_id, risk_level: query.risk_level, from: query.from, to: query.to,
      limit: PAGE_SIZE, offset: query.offset } }).then((data) => {
      results.replaceChildren(
        table([
          { label: "Date", cell: (r) => formatDate(r.tx_date) },
          { label: "Transaction", cell: (r) => r.transaction_id },
          { label: "Account", cell: (r) => r.account_id },
          { label: "Type", cell: (r) => r.tx_type === "CREDIT" ? "Credit" : "Debit" },
          { label: "Amount", number: true, cell: (r) => formatMoney(r.amount) },
          { label: "Country", cell: (r) => r.country_code },
          { label: "Merchant", cell: (r) => r.merchant_name || "" },
          { label: "Score", number: true, cell: (r) => r.risk_score },
          { label: "Risk", cell: (r) => riskPill(r.risk_level) },
        ], data.items, "No transactions match these filters.", (row) => openJourney(row.transaction_id, row)),
        pager(data.pagination, search));
    }).catch((error) => handleFailure(error, results));
  };

  // ------------------------------------------------------------------- alerts

  views.alerts = function (page, filter) {
    const query = Object.assign({ status: "OPEN", offset: 0 }, filter);
    const status = el("select", null, [["OPEN", "Open"], ["RESOLVED", "Resolved"], ["", "All"]]
      .map(([value, label]) => el("option", { value, selected: value === query.status }, label)));
    status.addEventListener("change", () => views.alerts(page, { status: status.value }));
    const results = el("div", null, el("p", { class: "notice", text: "Loading…" }));

    page.replaceChildren(
      pageHead("Alerts", "One alert is raised for every transaction scored High. Resolve an alert once it has been checked."),
      el("div", { class: "filters" }, field("Show", status)), results);

    api("GET", "/alerts", { params: { status: query.status, limit: PAGE_SIZE, offset: query.offset } }).then((data) => {
      results.replaceChildren(
        table([
          { label: "Raised", cell: (r) => formatDate(r.created_at) },
          { label: "Transaction", cell: (r) => r.transaction_id },
          { label: "Account", cell: (r) => r.account_id },
          { label: "Amount", number: true, cell: (r) => formatMoney(r.amount) },
          { label: "Country", cell: (r) => r.country_code },
          { label: "Score", number: true, cell: (r) => r.risk_score },
          { label: "Status", cell: (r) => statusPill(r.status) },
          { label: "Resolved by", wrap: true, cell: (r) => r.resolved_by
            ? `${r.resolved_by}, ${formatDate(r.resolved_at)}${r.resolution_note ? ": " + r.resolution_note : ""}` : "" },
          { label: "", cell: (r) => r.status === "OPEN"
            ? el("button", { type: "button", class: "button small", onclick: () => resolveAlert(r, page, query) }, "Resolve") : "" },
        ], data.items, query.status === "OPEN" ? "No open alerts. Nothing is waiting for review." : "No alerts to show.",
        (row) => openJourney(row.transaction_id, row)),
        pager(data.pagination, (offset) => views.alerts(page, { status: query.status, offset })));
    }).catch((error) => handleFailure(error, results));
  };

  function resolveAlert(alert, page, query) {
    const who = el("input", { type: "text", required: true, maxlength: "60", spellcheck: "false",
      placeholder: "n.hlongwane", pattern: "[A-Za-z0-9][A-Za-z0-9._\\-]{1,59}",
      title: "2 to 60 letters, digits, dots, dashes or underscores", value: sessionStorage.getItem("fintrust.user") || "" });
    const note = el("textarea", { maxlength: "500" });
    openDialog({
      title: "Resolve alert",
      text: `Transaction ${alert.transaction_id} on ${alert.account_id}, ${formatMoney(alert.amount)}.`,
      fields: [field("Your username", who), field("What was checked (optional)", note)],
      confirm: "Resolve alert",
      action: async () => {
        await api("POST", "/resolve", { body: {
          alert_id: alert.alert_id, resolved_by: who.value.trim(), resolution_note: note.value.trim() || undefined } });
        sessionStorage.setItem("fintrust.user", who.value.trim());
        toast("Alert resolved");
        views.alerts(page, query);
        refreshAlertCount();
      },
    });
  }

  // ----------------------------------------------------------------- accounts

  views.accounts = function (page, filter) {
    const query = Object.assign({ status: "", offset: 0 }, filter);
    const status = el("select", null, [["", "Any"], ["ACTIVE", "Active"], ["FLAGGED", "Flagged"], ["CLOSED", "Closed"]]
      .map(([value, label]) => el("option", { value, selected: value === query.status }, label)));
    status.addEventListener("change", () => views.accounts(page, { status: status.value }));
    const results = el("div", null, el("p", { class: "notice", text: "Loading…" }));

    page.replaceChildren(
      pageHead("Accounts", "Ranked by total risk points: the sum of the risk scores of an account's transactions."),
      el("div", { class: "filters" }, field("Status", status)), results);

    api("GET", "/accounts", { params: { status: query.status, limit: PAGE_SIZE, offset: query.offset } }).then((data) => {
      results.replaceChildren(
        table([
          { label: "Account", cell: (r) => r.account_id },
          { label: "Type", cell: (r) => r.account_type.charAt(0) + r.account_type.slice(1).toLowerCase() },
          { label: "Status", cell: (r) => statusPill(r.account_status) },
          { label: "Transactions", number: true, cell: (r) => whole.format(r.transaction_count) },
          { label: "High risk", number: true, cell: (r) => whole.format(r.high_risk_count) },
          { label: "Risk points", number: true, cell: (r) => whole.format(r.total_risk_points) },
          { label: "Open alerts", number: true, cell: (r) => whole.format(r.open_alerts) },
          { label: "Manual flags", number: true, cell: (r) => whole.format(r.manual_flags) },
          { label: "Last transaction", cell: (r) => formatDate(r.last_transaction_at) },
          { label: "", cell: (r) => r.account_status === "CLOSED" ? ""
            : el("button", { type: "button", class: "button small", onclick: () => flagAccount(r, page, query) }, "Flag") },
        ], data.items, "No accounts match this filter.",
        (row) => { go("transactions"); views.transactions($("page"), { account_id: row.account_id }); }),
        pager(data.pagination, (offset) => views.accounts(page, { status: query.status, offset })));
    }).catch((error) => handleFailure(error, results));
  };

  function flagAccount(account, page, query) {
    const reason = el("textarea", { required: true, maxlength: "500" });
    openDialog({
      title: "Flag account " + account.account_id,
      text: "The reason is recorded with the flag and cannot be edited afterwards.",
      fields: [field("Reason", reason)],
      confirm: "Flag account",
      action: async () => {
        await api("POST", "/flag", { body: { account_id: account.account_id, reason: reason.value.trim() } });
        toast("Account flagged");
        views.accounts(page, query);
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
      el("p", { class: "lede section", text: "The first line must name the columns: transaction_id, account_id, tx_date, amount, tx_type, channel and country_code are required. Rows that fail a check are set aside with the reason and are not stored." }),
      result);
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
          if (error.status === 403) { handleFailure(error, container); return; }
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
      } else if (error.status === 403) {
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
      if (error.status === 403) { $("dialog").close(); handleFailure(error, $("page")); return; }
      $("dialog-error").textContent = error.message;
      $("dialog-error").hidden = false;
    } finally {
      button.disabled = false;
      button.textContent = button.dataset.label;
    }
  }

  // -------------------------------------------------------------------- start

  $("connect-form").addEventListener("submit", connect);
  $("disconnect").addEventListener("click", disconnect);
  $("drawer-close").addEventListener("click", closeDrawer);
  $("scrim").addEventListener("click", closeDrawer);
  $("dialog-form").addEventListener("submit", submitDialog);
  $("dialog-cancel").addEventListener("click", () => $("dialog").close());
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !$("drawer").hidden) closeDrawer(); });
  for (const tab of document.querySelectorAll(".tab")) tab.addEventListener("click", () => go(tab.dataset.view));

  state.apiUrl = sessionStorage.getItem(STORAGE_URL) || (window.FINTRUST_CONFIG && window.FINTRUST_CONFIG.apiUrl) || "";
  state.apiKey = sessionStorage.getItem(STORAGE_KEY) || "";
  if (state.apiUrl && state.apiKey) showApp(); else showConnect();
})();
