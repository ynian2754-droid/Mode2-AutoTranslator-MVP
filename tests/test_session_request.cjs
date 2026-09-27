"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../static/session.js"), "utf8");

function page(fetchStub) {
  const window = { location: { href: "http://127.0.0.1:4873/editor", origin: "http://127.0.0.1:4873" } };
  const context = { window, location: window.location, fetch: fetchStub, Headers, URL };
  vm.runInNewContext(source, context);
  return window.Mode2Request;
}

function answer(status, data) {
  return { status, ok: status < 400, clone() { return this; }, async json() { return data; } };
}

test("a restarted service refreshes the in-memory token and retries once", async () => {
  const calls = [];
  const helper = page(async (url, options = {}) => {
    calls.push({ url, options });
    if (url === "/api/session") return answer(200, { token: calls.length === 1 ? "old" : "new" });
    if (calls.length === 2) return answer(403, { code: "mode2_token_invalid" });
    return answer(200, { status: "ok" });
  });
  const response = await helper.fetch("/api/pipeline/stop", { method: "POST" });
  assert.equal(response.status, 200);
  assert.deepEqual(calls.map(call => call.url), [
    "/api/session", "/api/pipeline/stop", "/api/session", "/api/pipeline/stop",
  ]);
  assert.equal(new Headers(calls[1].options.headers).get("X-Mode2-Token"), "old");
  assert.equal(new Headers(calls[3].options.headers).get("X-Mode2-Token"), "new");
});

test("a second token rejection never retries again", async () => {
  const calls = [];
  const helper = page(async (url, options = {}) => {
    calls.push({ url, options });
    return url === "/api/session"
      ? answer(200, { token: `token-${calls.length}` })
      : answer(403, { code: "mode2_token_invalid" });
  });
  const response = await helper.fetch("/api/pipeline/start", { method: "POST" });
  assert.equal(response.status, 403);
  assert.equal(calls.length, 4);
});

test("ordinary GETs need no bootstrap and multipart is left to fetch", async () => {
  const calls = [];
  const helper = page(async (url, options = {}) => {
    calls.push({ url, options });
    return url === "/api/session" ? answer(200, { token: "fresh" }) : answer(200, {});
  });
  await helper.fetch("/api/projects");
  assert.equal(calls.length, 1);
  const body = new FormData();
  body.append("file", "source text");
  await helper.fetch("/api/projects/import", { method: "POST", body });
  const headers = new Headers(calls[2].options.headers);
  assert.equal(headers.get("X-Mode2-Token"), "fresh");
  assert.equal(headers.has("Content-Type"), false);
});

test("non-session 403s are not retried and tokens stay on this API", async () => {
  const calls = [];
  const helper = page(async (url, options = {}) => {
    calls.push({ url, options });
    return url === "/api/session" ? answer(200, { token: "fresh" }) : answer(403, { detail: "Forbidden" });
  });
  assert.equal((await helper.fetch("/api/pipeline/stop", { method: "POST" })).status, 403);
  assert.equal(calls.length, 2);
  await assert.rejects(helper.fetch("https://other.example/collect", { method: "POST" }));
  assert.equal(calls.length, 2);
});
