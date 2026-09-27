"use strict";

// One page-memory token for all Mode2 API writes. GET requests remain ordinary
// same-origin reads; a rejected write is replayed only after an explicit 403.
(() => {
  let token = null;
  let pendingSession = null;
  const sessionError = () => new Error(window.Mode2I18n?.locale === "en"
    ? "Unable to establish a Mode2 session. Reload this page."
    : "无法建立 Mode2 会话，请刷新页面。");

  async function sessionToken(refresh = false) {
    if (refresh) token = null;
    if (token) return token;
    if (!pendingSession) {
      pendingSession = fetch("/api/session", { cache: "no-store", credentials: "same-origin" })
        .then(async (response) => {
          if (!response.ok) throw sessionError();
          const data = await response.json();
          if (typeof data.token !== "string" || !data.token) {
            throw sessionError();
          }
          token = data.token;
          return token;
        })
        .finally(() => { pendingSession = null; });
    }
    return pendingSession;
  }

  async function mode2Fetch(path, options = {}) {
    const method = String(options.method || "GET").toUpperCase();
    if (method === "GET" || method === "HEAD" || method === "OPTIONS") {
      return fetch(path, options);
    }
    const target = new URL(path, window.location.href);
    if (target.origin !== window.location.origin || !target.pathname.startsWith("/api/")) {
      throw new Error("Mode2 writes must use the local API.");
    }
    const send = async (refresh = false) => {
      const headers = new Headers(options.headers || {});
      headers.set("X-Mode2-Token", await sessionToken(refresh));
      return fetch(path, { ...options, headers });
    };
    let response = await send();
    if (response.status === 403) {
      const error = await response.clone().json().catch(() => ({}));
      if (error.code === "mode2_token_invalid") response = await send(true);
    }
    return response;
  }

  window.Mode2Request = { fetch: mode2Fetch };
})();
