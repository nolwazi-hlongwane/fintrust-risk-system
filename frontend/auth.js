/* FinTrust risk desk: sign-in with Amazon Cognito (Week 15 Day 2).
 *
 * The OAuth 2.0 authorization code flow with PKCE, the standard for a web page
 * that cannot keep a secret:
 *
 *   1. "Sign in" sends the browser to Cognito's managed login page, with a
 *      random `state` and a PKCE challenge: the SHA-256 hash of a random
 *      `verifier` that never leaves this tab.
 *   2. The person enters their username, password and authenticator code on
 *      Cognito's page. This file never sees the password.
 *   3. Cognito sends the browser back with a one-time `code`. `state` must
 *      match, or the response is refused (it was not this tab that asked).
 *   4. The code and the verifier are exchanged for tokens. Someone who stole the
 *      code could not use it: they do not have the verifier.
 *
 * The tokens are kept in sessionStorage: this tab only, gone when it closes.
 * The ID token lasts 30 minutes and is renewed with the refresh token (8 hours)
 * shortly before it expires. Signing out revokes the refresh token at Cognito,
 * and Cognito's logout page clears its own sign-in cookie.
 *
 * Plain JavaScript, no library: the browser's own crypto.subtle does SHA-256.
 */
"use strict";

window.FinTrustAuth = (function () {
  const TOKENS = "fintrust.tokens";
  const PENDING = "fintrust.signin";
  const RENEW_BEFORE_MS = 2 * 60 * 1000;      // renew the ID token when it has less than 2 minutes left

  const settings = () => (window.FINTRUST_CONFIG && window.FINTRUST_CONFIG.signIn) || {};
  const returnAddress = () => window.location.origin + "/";

  class SignInError extends Error {}

  function configured() {
    const { domain, clientId } = settings();
    // HTTPS always; plain http only for a sign-in service on this computer (local development).
    return Boolean(domain && clientId && /^(https:\/\/|http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$)/.test(domain));
  }

  // ---------------------------------------------------------------- storage

  function read(key) {
    try { return JSON.parse(window.sessionStorage.getItem(key) || "null"); } catch (_error) { return null; }
  }
  function write(key, value) {
    try { window.sessionStorage.setItem(key, JSON.stringify(value)); } catch (_error) { /* private mode: lasts for this page */ }
  }
  function remove(key) {
    try { window.sessionStorage.removeItem(key); } catch (_error) { /* nothing stored */ }
  }

  // ------------------------------------------------------------------- PKCE

  function base64url(bytes) {
    let text = "";
    for (const byte of new Uint8Array(bytes)) text += String.fromCharCode(byte);
    return window.btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function randomText(size) {
    return base64url(window.crypto.getRandomValues(new Uint8Array(size)));
  }
  async function challengeFor(verifier) {
    return base64url(await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  }

  /** Step 1: go to Cognito's sign-in page. `returnTo` is the dashboard view to reopen afterwards. */
  async function signIn(returnTo) {
    if (!configured()) throw new SignInError("Sign-in is not set up for this dashboard yet.");
    const verifier = randomText(64);
    const state = randomText(24);
    write(PENDING, { verifier, state, returnTo: returnTo || "", started: Date.now() });
    const { domain, clientId } = settings();
    const address = new URL(domain.replace(/\/+$/, "") + "/oauth2/authorize");
    address.search = new URLSearchParams({
      response_type: "code", client_id: clientId, redirect_uri: returnAddress(),
      scope: "openid email profile", state, code_challenge: await challengeFor(verifier),
      code_challenge_method: "S256",
    }).toString();
    window.location.assign(address.toString());
  }

  async function tokenRequest(fields) {
    const { domain } = settings();
    let response;
    try {
      response = await fetch(domain.replace(/\/+$/, "") + "/oauth2/token", {
        method: "POST", cache: "no-store",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams(fields).toString(),
      });
    } catch (_error) {
      throw new SignInError("The sign-in service could not be reached. Check your connection and try again.");
    }
    let data = null;
    try { data = await response.json(); } catch (_error) { /* not JSON */ }
    if (!response.ok || !data || !data.id_token) {
      throw new SignInError(data && data.error === "invalid_grant"
        ? "That sign-in link has expired. Sign in again."
        : "Sign-in could not be completed. Sign in again.");
    }
    return data;
  }

  function keep(data, refreshToken) {
    const tokens = {
      idToken: data.id_token,
      refreshToken: data.refresh_token || refreshToken,
      expiresAt: Date.now() + (Number(data.expires_in) || 1800) * 1000,
    };
    write(TOKENS, tokens);
    return tokens;
  }

  /**
   * Steps 3 and 4, when Cognito has sent the browser back. Returns
   * { returnTo } after a sign-in, or null if this page load is not a return
   * from Cognito. Throws SignInError if it is one but cannot be accepted.
   */
  async function completeSignIn() {
    const query = new URLSearchParams(window.location.search);
    if (!query.has("code") && !query.has("error")) return null;
    const pending = read(PENDING);
    remove(PENDING);
    // Remove the code from the address bar and the browser history straight away.
    window.history.replaceState(null, "", window.location.pathname + window.location.hash);

    if (query.has("error")) {
      throw new SignInError(query.get("error") === "access_denied"
        ? "Sign-in was cancelled." : "Sign-in did not complete. Try again.");
    }
    if (!pending || pending.state !== query.get("state")) {
      throw new SignInError("That sign-in response was not started from this tab, so it was not accepted. Sign in again.");
    }
    const { clientId } = settings();
    keep(await tokenRequest({
      grant_type: "authorization_code", client_id: clientId, code: query.get("code"),
      redirect_uri: returnAddress(), code_verifier: pending.verifier,
    }));
    return { returnTo: pending.returnTo };
  }

  // ----------------------------------------------------------------- tokens

  let renewing = null;      // one renewal at a time, shared by every caller

  async function renew() {
    const tokens = read(TOKENS);
    if (!tokens || !tokens.refreshToken) throw new SignInError("Your sign-in has ended. Sign in again.");
    if (!renewing) {
      const { clientId } = settings();
      renewing = tokenRequest({ grant_type: "refresh_token", client_id: clientId, refresh_token: tokens.refreshToken })
        .then((data) => keep(data, tokens.refreshToken))
        .finally(() => { renewing = null; });
    }
    return renewing;
  }

  /** A valid ID token for the Authorization header, renewed first if it is about to expire. */
  async function idToken(force) {
    let tokens = read(TOKENS);
    if (!tokens) throw new SignInError("You are not signed in.");
    if (force || tokens.expiresAt - Date.now() < RENEW_BEFORE_MS) tokens = await renew();
    return tokens.idToken;
  }

  function hasTokens() {
    return Boolean(read(TOKENS));
  }

  /** The claims inside the ID token, for display only. The API checks the token itself. */
  function claims() {
    const tokens = read(TOKENS);
    if (!tokens) return null;
    try {
      const part = tokens.idToken.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
      const text = window.atob(part + "=".repeat((4 - (part.length % 4)) % 4));
      return JSON.parse(decodeURIComponent(Array.from(text, (c) => "%" + c.charCodeAt(0).toString(16).padStart(2, "0")).join("")));
    } catch (_error) {
      return null;
    }
  }

  // --------------------------------------------------------------- sign out

  /**
   * Revoke the refresh token, forget the tokens, and let Cognito clear its own
   * sign-in cookie. The browser comes back to the dashboard's sign-in screen.
   */
  async function signOut() {
    const tokens = read(TOKENS);
    remove(TOKENS);
    remove(PENDING);
    if (!configured()) { window.location.assign(returnAddress()); return; }
    const { domain, clientId } = settings();
    const base = domain.replace(/\/+$/, "");
    if (tokens && tokens.refreshToken) {
      try {
        await fetch(base + "/oauth2/revoke", {
          method: "POST", cache: "no-store", headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({ token: tokens.refreshToken, client_id: clientId }).toString(),
        });
      } catch (_error) { /* the refresh token still expires by itself within 8 hours */ }
    }
    const logout = new URL(base + "/logout");
    logout.search = new URLSearchParams({ client_id: clientId, logout_uri: returnAddress() }).toString();
    window.location.assign(logout.toString());
  }

  /** Forget the tokens in this tab without leaving the page (used when the API says the session is over). */
  function forget() {
    remove(TOKENS);
    remove(PENDING);
  }

  return { configured, signIn, completeSignIn, idToken, hasTokens, claims, signOut, forget, SignInError };
})();
