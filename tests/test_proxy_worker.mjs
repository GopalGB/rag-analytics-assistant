import test from "node:test";
import assert from "node:assert/strict";
import worker from "../deploy/rag-assistant-worker.mjs";

test("forwards exact prefix, query, body, and safe headers", async () => {
  let seen; const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => { seen = { url: String(url), init }; return new Response("ok"); };
  try {
    const response = await worker.fetch(new Request("https://gopalbagaswar.com/rag-assistant/chat?q=1", { method: "POST", headers: { "content-type": "application/json", authorization: "secret", cookie: "x", "x-api-key": "y" }, body: '{"x":1}' }), { VERCEL_ORIGIN: "https://rag-analytics-assistant.vercel.app", ORIGIN_API_KEY: "server-secret" });
    assert.equal(await response.text(), "ok"); assert.equal(seen.url, "https://rag-analytics-assistant.vercel.app/chat?q=1");
    assert.equal(seen.init.headers.get("authorization"), null); assert.equal(seen.init.headers.get("cookie"), null); assert.equal(seen.init.headers.get("x-api-key"), "server-secret"); assert.equal(await new Response(seen.init.body).text(), '{"x":1}');
  } finally { globalThis.fetch = original; }
});
test("redirects base and rejects lookalikes", async () => {
  assert.equal((await worker.fetch(new Request("https://gopalbagaswar.com/rag-assistant"), { VERCEL_ORIGIN: "https://example.com" })).status, 308);
  assert.equal((await worker.fetch(new Request("https://gopalbagaswar.com/rag-assistant-extra"), { VERCEL_ORIGIN: "https://example.com" })).status, 404);
});
test("sanitizes upstream failures", async () => {
  const original = globalThis.fetch; globalThis.fetch = async () => new Response("secret stack", { status: 500 });
  try { const response = await worker.fetch(new Request("https://gopalbagaswar.com/rag-assistant/health"), { VERCEL_ORIGIN: "https://example.com", ORIGIN_API_KEY: "server-secret" }); assert.equal(response.status, 502); assert.equal(await response.text(), "Upstream unavailable"); }
  finally { globalThis.fetch = original; }
});
test("preserves safe upstream client errors and rejects unsupported methods", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => new Response("bad request details", { status: 429, headers: { "x-test": "ok" } });
  try {
    const response = await worker.fetch(new Request("https://gopalbagaswar.com/rag-assistant/chat", { method: "POST" }), { VERCEL_ORIGIN: "https://example.com", ORIGIN_API_KEY: "server-secret" });
    assert.equal(response.status, 429);
    assert.equal(await response.text(), "bad request details");
    assert.equal((await worker.fetch(new Request("https://gopalbagaswar.com/rag-assistant/chat", { method: "PUT" }), { VERCEL_ORIGIN: "https://example.com" })).status, 404);
  } finally { globalThis.fetch = original; }
});
test("strips spoofed forwarding headers", async () => {
  let seen; const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => { seen = init.headers; return new Response("ok"); };
  try {
    await worker.fetch(new Request("https://gopalbagaswar.com/rag-assistant/health", { headers: { "x-forwarded-host": "evil.example", "x-forwarded-for": "1.2.3.4" } }), { VERCEL_ORIGIN: "https://example.com", ORIGIN_API_KEY: "server-secret" });
    assert.equal(seen.get("x-forwarded-host"), "gopalbagaswar.com");
    assert.equal(seen.get("x-forwarded-for"), null);
  } finally { globalThis.fetch = original; }
});
test("fails closed when the origin key is missing", async () => {
  const original = globalThis.fetch; let called = false;
  globalThis.fetch = async () => { called = true; return new Response("unexpected"); };
  try {
    const response = await worker.fetch(new Request("https://gopalbagaswar.com/rag-assistant/health"), { VERCEL_ORIGIN: "https://example.com" });
    assert.equal(response.status, 503); assert.equal(await response.text(), "Proxy unavailable"); assert.equal(response.headers.get("cache-control"), "no-store"); assert.equal(called, false);
  } finally { globalThis.fetch = original; }
});
