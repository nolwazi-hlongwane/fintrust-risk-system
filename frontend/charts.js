/* FinTrust risk desk: the charts.
 *
 * Small SVG charts drawn with plain JavaScript. There is no chart library, so
 * the Content-Security-Policy can still forbid every script but the site's own,
 * and every line here can be explained.
 *
 * This file only turns numbers into shapes. Every number comes from the API
 * (PostgreSQL did the counting); nothing is added up or invented here.
 *
 * The same safety rule as app.js: text is always set with textContent, never
 * as HTML, so a value from the API can never run as script.
 *
 * Two kinds of chart:
 *   columnChart  bars per day/week/bucket, stacked or side by side (activity, alerts, scores)
 *   stepChart    a balance that changes at each transaction (running balance)
 * Both redraw when their box changes size, answer the mouse and the keyboard
 * (arrow keys move, Enter selects), and show one tooltip.
 */
"use strict";

window.FinTrustCharts = (function () {
  const SVG = "http://www.w3.org/2000/svg";
  const FONT_PX = 12;                     // axis text size; used to estimate label widths
  const GAP = 2;                          // the surface-coloured gap between touching marks
  const MAX_COLUMN = 24;                  // columns never get thicker than this

  /** Build an SVG element. Children may be elements or plain text (added safely as text). */
  function s(tag, attributes, ...children) {
    const node = document.createElementNS(SVG, tag);
    for (const [name, value] of Object.entries(attributes || {})) {
      if (value !== null && value !== undefined && value !== false) node.setAttribute(name, String(value));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function h(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // ---------------------------------------------------------------- scales

  /** Round axis values (0, 5, 10 ... or 0, 200k, 400k ...) that cover min..max in about `ticks` steps.
   *  Columns always start at zero; a line may start higher (includeZero false). */
  function niceScale(minimum, maximum, ticks, includeZero) {
    let low = includeZero === false ? minimum : Math.min(0, minimum);
    let high = Math.max(maximum, low + 1);
    const rough = (high - low) / (ticks || 4);
    const magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((candidate) => candidate >= rough);
    low = Math.floor(low / step) * step;
    high = Math.ceil(high / step) * step;
    const values = [];
    for (let value = low; value <= high + step / 2; value += step) values.push(Math.round(value * 100) / 100);
    return { min: low, max: high, step, values };
  }

  // --------------------------------------------------------------- tooltip

  function tip() { return document.getElementById("chart-tip"); }

  /** Show the one tooltip next to `anchor` (a DOMRect). rows: [{value, label, key}] where key is a CSS class. */
  function showTip(anchor, title, rows) {
    const box = tip();
    if (!box) return;
    const list = h("div", "tip-rows");
    for (const row of rows) {
      const line = h("div", "tip-row");
      if (row.key) line.append(h("span", "tip-key " + row.key));
      line.append(h("strong", null, row.value), h("span", "tip-label", row.label));
      list.append(line);
    }
    box.replaceChildren(h("div", "tip-title", title), list);
    box.hidden = false;
    const width = box.offsetWidth;
    const height = box.offsetHeight;
    const left = Math.min(Math.max(8, anchor.left + anchor.width / 2 - width / 2), window.innerWidth - width - 8);
    let top = anchor.top - height - 10;
    if (top < 8) top = Math.min(anchor.bottom + 10, window.innerHeight - height - 8);
    box.style.left = left + "px";            // CSSOM, which the Content-Security-Policy allows
    box.style.top = top + "px";
  }

  function hideTip() {
    const box = tip();
    if (box) box.hidden = true;
  }

  // --------------------------------------------------------------- resizing

  // One observer for every chart: when a chart's box changes width, it redraws
  // at the new size, so text stays 12px on a phone and on a wide screen.
  const resized = new ResizeObserver((entries) => {
    for (const entry of entries) {
      const host = entry.target;
      if (host.isConnected && host._draw && Math.round(entry.contentRect.width) !== host._drawnWidth) host._draw();
    }
  });

  function attach(host, draw, label, onKey) {
    host.classList.add("chart");
    host.tabIndex = 0;
    host.setAttribute("role", "group");
    host.setAttribute("aria-roledescription", "chart");
    host.setAttribute("aria-label", label + ". Use the left and right arrow keys to read each value.");
    host._draw = () => { host._drawnWidth = Math.round(host.clientWidth); draw(); };
    host.addEventListener("keydown", onKey);
    host.addEventListener("blur", hideTip);
    resized.observe(host);
    host._draw();
  }

  /** Move the keyboard cursor: arrows, Home, End. Returns the new index or null. */
  function stepKey(event, index, count) {
    const moves = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: count - 1 };
    if (!(event.key in moves)) return null;
    event.preventDefault();
    return Math.min(count - 1, Math.max(0, moves[event.key]));
  }

  /** A column whose top corners are rounded and whose foot is square on the baseline. */
  function roundedTop(x, y, width, height, cls) {
    const r = Math.max(0, Math.min(4, width / 2, height));
    return s("path", {
      class: cls,
      d: `M${x},${y + height} V${y + r} Q${x},${y} ${x + r},${y} H${x + width - r} Q${x + width},${y} ${x + width},${y + r} V${y + height} Z`,
    });
  }

  /** Which of `count` labels to print so they never run into each other. */
  function labelStep(count, room, longest) {
    const fits = Math.max(1, Math.floor(room / (longest * FONT_PX * 0.62 + 14)));
    return Math.max(1, Math.ceil(count / fits));
  }

  // ----------------------------------------------------------- column chart

  /**
   * options:
   *   labels      axis label per column            titles   tooltip title per column
   *   series      [{key, name, cls}] bottom first  value(i, key) -> number
   *   stacked     true: one stacked column; false: the series side by side
   *   rows(i)     tooltip rows for column i        tick(v)  axis number format
   *   markers     [{position, label}] vertical reference lines, position in column widths from the left
   *   onSelect(i) called on click or Enter         label    what the chart shows, for screen readers
   */
  function columnChart(host, options) {
    const count = options.labels.length;
    let active = -1;
    let bands = [];

    function setActive(index) {
      active = index;
      bands.forEach((band, i) => band.classList.toggle("on", i === index));
      if (index < 0) { hideTip(); return; }
      showTip(bands[index].querySelector(".hit").getBoundingClientRect(), options.titles[index], options.rows(index));
    }

    function draw() {
      const width = Math.max(240, host.clientWidth);
      const height = options.height || 220;
      const totals = options.labels.map((_, i) => options.stacked
        ? options.series.reduce((sum, item) => sum + options.value(i, item.key), 0)
        : Math.max(...options.series.map((item) => options.value(i, item.key))));
      const scale = niceScale(0, Math.max(1, ...totals), 4);
      const tick = options.tick || String;
      const left = Math.max(...scale.values.map((v) => tick(v, scale.step).length)) * FONT_PX * 0.62 + 14;
      const top = options.markers ? 22 : 10;
      const bottom = 26;
      const plotWidth = width - left - 8;
      const plotHeight = height - top - bottom;
      const band = plotWidth / count;
      const y = (value) => top + plotHeight - (value - scale.min) / (scale.max - scale.min) * plotHeight;

      const svg = s("svg", { width, height, viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true", focusable: "false" });
      for (const value of scale.values) {
        svg.append(s("line", { class: value === 0 ? "axis" : "grid", x1: left, x2: width - 8, y1: y(value), y2: y(value) }),
          s("text", { class: "tick", x: left - 8, y: y(value) + 4, "text-anchor": "end" }, tick(value, scale.step)));
      }

      const columnWidth = options.stacked
        ? Math.min(MAX_COLUMN, Math.max(2, band - Math.max(GAP, band * 0.28)))
        : Math.min(MAX_COLUMN / 2 + 2, Math.max(2, (band - Math.max(GAP * 2, band * 0.3)) / options.series.length - GAP / 2));
      bands = options.labels.map((_, i) => {
        const group = s("g", { class: "band" + (options.onSelect ? " selectable" : "") });
        const x0 = left + i * band;
        group.append(s("rect", { class: "wash", x: x0, y: top, width: band, height: plotHeight }));
        const centre = x0 + band / 2;
        if (options.stacked) {
          const visible = options.series.filter((item) => options.value(i, item.key) > 0);
          let base = 0;
          visible.forEach((item, position) => {
            const value = options.value(i, item.key);
            const y0 = y(base);
            const y1 = Math.min(y(base + value), y0 - 2);     // at least 2px, so a 1 is never invisible
            const isTop = position === visible.length - 1;
            const shapeTop = isTop ? y1 : y1 + GAP;          // a surface gap under the segment above
            if (y0 - shapeTop > 0) {
              group.append(isTop
                ? roundedTop(centre - columnWidth / 2, shapeTop, columnWidth, y0 - shapeTop, "mark " + item.cls)
                : s("rect", { class: "mark " + item.cls, x: centre - columnWidth / 2, y: shapeTop, width: columnWidth, height: y0 - shapeTop }));
            }
            base += value;
          });
        } else {
          const groupWidth = options.series.length * columnWidth + (options.series.length - 1) * GAP;
          options.series.forEach((item, position) => {
            const value = options.value(i, item.key);
            if (!value) return;
            const x = centre - groupWidth / 2 + position * (columnWidth + GAP);
            const yTop = Math.min(y(value), y(0) - 2);
            group.append(roundedTop(x, yTop, columnWidth, y(0) - yTop, "mark " + item.cls));
          });
        }
        group.append(s("rect", { class: "hit", x: x0, y: top, width: band, height: plotHeight + bottom }));
        group.addEventListener("pointerenter", () => setActive(i));
        if (options.onSelect) group.addEventListener("click", () => options.onSelect(i));
        svg.append(group);
        return group;
      });

      const longest = Math.max(...options.labels.map((label) => label.length));
      const every = labelStep(count, plotWidth, longest);
      options.labels.forEach((label, i) => {
        if (i % every === 0) {
          svg.append(s("text", { class: "tick", x: left + i * band + band / 2, y: height - 8, "text-anchor": "middle" }, label));
        }
      });

      for (const marker of options.markers || []) {
        const x = left + marker.position * band;
        svg.append(s("line", { class: "marker", x1: x, x2: x, y1: top - 4, y2: top + plotHeight }),
          s("text", { class: "marker-label", x: x + 4, y: top - 8 }, marker.label));
      }

      svg.addEventListener("pointerleave", () => { if (document.activeElement !== host) setActive(-1); });
      host.replaceChildren(svg);
      if (active >= 0 && document.activeElement === host) setActive(active);
    }

    attach(host, draw, options.label, (event) => {
      if (event.key === "Enter" && active >= 0 && options.onSelect) { options.onSelect(active); return; }
      if (event.key === "Escape") { setActive(-1); return; }
      const next = stepKey(event, active, count);
      if (next !== null) setActive(next);
    });
  }

  // ------------------------------------------------------------- step chart

  /**
   * A balance over time. Between two transactions the balance does not change,
   * so the line is drawn as steps, not slopes.
   *
   * options:
   *   points       [{t (ms), v, highlight, title, rows}] oldest first
   *   opening      {t, v} optional: the balance at the start of the chosen period, so the
   *                line starts there (one transaction in a period is still a visible step)
   *   until        t optional: carry the last balance to the end of the chosen period
   *   tick(v, step) axis number format         date(t)  axis date format
   *   onSelect(i)  click or Enter on a point   label    for screen readers
   */
  function stepChart(host, options) {
    const points = options.points;
    let active = -1;
    let place = () => null;
    let cursor = null;

    function setActive(index) {
      active = index;
      if (index < 0 || !cursor) { if (cursor) cursor.group.classList.remove("on"); hideTip(); return; }
      const at = place(index);
      cursor.group.classList.add("on");
      cursor.line.setAttribute("x1", at.x); cursor.line.setAttribute("x2", at.x);
      cursor.dot.setAttribute("cx", at.x); cursor.dot.setAttribute("cy", at.y);
      const box = cursor.dot.getBoundingClientRect();
      showTip(box, points[index].title, points[index].rows);
    }

    function draw() {
      const width = Math.max(240, host.clientWidth);
      const height = options.height || 240;
      const opening = options.opening && options.opening.t < points[0].t ? options.opening : null;
      const values = points.map((p) => p.v).concat(opening ? [opening.v] : []);
      const low = Math.min(...values);
      const high = Math.max(...values);
      // A balance that dips below zero, or comes close to it, is drawn with zero on the axis.
      const scale = niceScale(low, high, 4, low <= 0 || low < high * 0.25);
      const tick = options.tick || String;
      const left = Math.max(...scale.values.map((v) => tick(v, scale.step).length)) * FONT_PX * 0.62 + 14;
      const top = 12;
      const bottom = 26;
      const right = 14;
      const plotWidth = width - left - right;
      const plotHeight = height - top - bottom;
      let t0 = opening ? opening.t : points[0].t;
      let t1 = Math.max(points[points.length - 1].t, options.until || 0);
      if (t1 === t0) { t0 -= 43200000; t1 += 43200000; }    // one point: half a day either side
      const x = (t) => left + (t - t0) / (t1 - t0) * plotWidth;
      const y = (v) => top + plotHeight - (v - scale.min) / (scale.max - scale.min) * plotHeight;
      place = (i) => ({ x: x(points[i].t), y: y(points[i].v) });

      const svg = s("svg", { width, height, viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true", focusable: "false" });
      for (const value of scale.values) {
        if (value < scale.min || value > scale.max) continue;
        svg.append(s("line", { class: value === 0 ? "axis" : "grid", x1: left, x2: width - right, y1: y(value), y2: y(value) }),
          s("text", { class: "tick", x: left - 8, y: y(value) + 4, "text-anchor": "end" }, tick(value, scale.step)));
      }

      const ticks = Math.max(2, Math.min(6, Math.floor(plotWidth / 90)));
      for (let n = 0; n < ticks; n += 1) {
        const t = t0 + (t1 - t0) * n / (ticks - 1);
        svg.append(s("text", { class: "tick", x: x(t), y: height - 8,
          "text-anchor": n === 0 ? "start" : n === ticks - 1 ? "end" : "middle" }, options.date(t)));
      }

      let path = opening ? `M${x(opening.t)},${y(opening.v)} H${x(points[0].t)} V${y(points[0].v)}`
        : `M${x(points[0].t)},${y(points[0].v)}`;
      for (let i = 1; i < points.length; i += 1) path += ` H${x(points[i].t)} V${y(points[i].v)}`;
      if (options.until && options.until > points[points.length - 1].t) path += ` H${x(options.until)}`;
      svg.append(s("path", { class: "line", d: path }));
      for (const point of points) {
        if (point.highlight) svg.append(s("circle", { class: "dot high", cx: x(point.t), cy: y(point.v), r: 4.5 }));
        else if (points.length < 3) svg.append(s("circle", { class: "dot plain", cx: x(point.t), cy: y(point.v), r: 4 }));
      }

      const group = s("g", { class: "cursor" });
      const line = s("line", { class: "crosshair", y1: top, y2: top + plotHeight });
      const dot = s("circle", { class: "dot focus", r: 5 });
      group.append(line, dot);
      svg.append(group);
      cursor = { group, line, dot };

      // The whole plot is the target: the pointer only has to be nearest a point, not on it.
      const hit = s("rect", { class: "hit" + (options.onSelect ? " selectable" : ""), x: left, y: top, width: plotWidth, height: plotHeight });
      const nearest = (event) => {
        const box = svg.getBoundingClientRect();
        const px = event.clientX - box.left;
        let best = 0;
        for (let i = 1; i < points.length; i += 1) {
          if (Math.abs(x(points[i].t) - px) < Math.abs(x(points[best].t) - px)) best = i;
        }
        return best;
      };
      hit.addEventListener("pointermove", (event) => { const i = nearest(event); if (i !== active) setActive(i); });
      hit.addEventListener("pointerleave", () => { if (document.activeElement !== host) setActive(-1); });
      if (options.onSelect) hit.addEventListener("click", (event) => options.onSelect(nearest(event)));
      svg.append(hit);
      host.replaceChildren(svg);
      if (active >= 0 && document.activeElement === host) setActive(active);
    }

    attach(host, draw, options.label, (event) => {
      if (event.key === "Enter" && active >= 0 && options.onSelect) { options.onSelect(active); return; }
      if (event.key === "Escape") { setActive(-1); return; }
      const next = stepKey(event, active, points.length);
      if (next !== null) setActive(next);
    });
  }

  // ----------------------------------------------------------------- legend

  /** A legend: [{name, cls, kind: "box" | "line" | "dot"}]. The key mirrors the mark. */
  function legend(items) {
    const list = h("ul", "chart-key");
    for (const item of items) {
      const entry = h("li");
      entry.append(h("span", `key-${item.kind || "box"} ${item.cls}`), document.createTextNode(item.name));
      list.append(entry);
    }
    return list;
  }

  return { columnChart, stepChart, legend, niceScale, hideTip };
})();
