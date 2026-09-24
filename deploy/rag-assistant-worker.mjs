const PREFIX = "/rag-assistant";
const METHODS = new Set(["GET", "HEAD", "POST", "OPTIONS"]);
const STRIP = ["authorization", "cookie", "set-cookie", "x-api-key", "host", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-prefix"];

export default { async fetch(request, env) {
  const incoming = new URL(request.url);
  if (incoming.pathname === PREFIX) return Response.redirect(`${incoming.origin}${PREFIX}/`, 308);
  if (!incoming.pathname.startsWith(`${PREFIX}/`) || !METHODS.has(request.method)) return new Response("Not found", { status: 404 });
  if (typeof env.ORIGIN_API_KEY !== "string" || !env.ORIGIN_API_KEY.trim()) return new Response("Proxy unavailable", { status: 503, headers: { "cache-control": "no-store" } });
  const target = new URL(env.VERCEL_ORIGIN);
  if (target.protocol !== "https:") return new Response("Bad upstream", { status: 500 });
  target.pathname = incoming.pathname.slice(PREFIX.length) || "/";
  target.search = incoming.search;
  const headers = new Headers(request.headers);
  STRIP.forEach((name) => headers.delete(name));
  headers.set("x-forwarded-host", incoming.host);
  headers.set("x-forwarded-prefix", PREFIX);
  headers.set("x-api-key", env.ORIGIN_API_KEY);
  let upstream;
  try { upstream = await fetch(target, { method: request.method, headers, body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body, redirect: "manual" }); }
  catch { return new Response("Upstream unavailable", { status: 502, headers: { "cache-control": "no-store" } }); }
  if (upstream.status >= 300 && upstream.status < 400) return new Response("Upstream redirect rejected", { status: 502, headers: { "cache-control": "no-store" } });
  if (upstream.status >= 500) return new Response("Upstream unavailable", { status: 502, headers: { "cache-control": "no-store" } });
  const responseHeaders = new Headers(upstream.headers);
  responseHeaders.delete("set-cookie");
  return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
} };
