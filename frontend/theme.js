/* FinTrust risk desk: light and dark appearance (Week 15 Day 2).
 *
 * Loaded in the <head>, before the page is drawn, so the page never flashes
 * the wrong colours. Three choices:
 *   system  follow the device's setting (the default)
 *   light   always light
 *   dark    always dark
 * The choice is remembered in this browser only (localStorage). It is a
 * preference, not data: if storage is blocked the page simply follows the device.
 */
"use strict";

window.FinTrustTheme = (function () {
  const KEY = "fintrust.theme";
  const CHOICES = ["system", "light", "dark"];
  const media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  const listeners = [];

  function saved() {
    try {
      const value = window.localStorage.getItem(KEY);
      return CHOICES.includes(value) ? value : "system";
    } catch (_error) {
      return "system";
    }
  }

  /** What the page actually shows: "light" or "dark". */
  function effective(choice) {
    if (choice === "light" || choice === "dark") return choice;
    return media && media.matches ? "dark" : "light";
  }

  function apply(choice) {
    const root = document.documentElement;
    if (choice === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", choice);
    root.style.colorScheme = effective(choice);          // native controls (date pickers, scrollbars) follow
    for (const listener of listeners) listener(choice, effective(choice));
  }

  function set(choice) {
    if (!CHOICES.includes(choice)) return;
    try { window.localStorage.setItem(KEY, choice); } catch (_error) { /* the choice still applies to this page */ }
    apply(choice);
  }

  apply(saved());
  // A device switching between light and dark (for example at sunset) is followed live.
  if (media && media.addEventListener) media.addEventListener("change", () => { if (saved() === "system") apply("system"); });

  return {
    choice: saved,
    effective: () => effective(saved()),
    set,
    /** Switch to the opposite of what is showing now. */
    toggle: () => set(effective(saved()) === "dark" ? "light" : "dark"),
    onChange: (listener) => listeners.push(listener),
  };
})();
