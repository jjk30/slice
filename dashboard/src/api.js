// Thin client for the gateway's read-only stats API. Base URL comes from
// VITE_API_BASE (see .env.example); it defaults to the local gateway so the
// dashboard works out of the box in dev.

const BASE = (import.meta.env.VITE_API_BASE || "http://localhost:8080/api").replace(/\/$/, "");

async function get(path) {
  const res = await fetch(BASE + path, { headers: { accept: "application/json" } });
  if (!res.ok) {
    throw new Error(`GET ${path} -> ${res.status}`);
  }
  return res.json();
}

// Mutating request (POST/DELETE). Same throw-on-non-2xx contract as get(), but it
// surfaces the gateway's own error message (e.g. a 400 "unknown to_model") so the
// UI can show why a save was rejected, falling back to a generic status line.
async function send(path, method, body) {
  const opts = { method, headers: { accept: "application/json" } };
  if (body !== undefined) {
    opts.headers["content-type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(BASE + path, opts);
  if (!res.ok) {
    let detail = "";
    try {
      detail = (await res.json())?.error || "";
    } catch {
      /* non-JSON error body — fall back to the status line below */
    }
    throw new Error(detail || `${method} ${path} -> ${res.status}`);
  }
  return res.json();
}

export const api = {
  summary: (days = 30) => get(`/summary?days=${days}`),
  spendByModel: (days = 30) => get(`/spend-by-model?days=${days}`),
  recent: (limit = 25) => get(`/recent?limit=${limit}`),
  spendDaily: (days = 30) => get(`/spend-daily?days=${days}`),
  budgets: () => get(`/budgets`),
  suggestions: (days = 30) => get(`/suggestions?days=${days}`),

  // Per-team switch-rules (default team in v1 — no team header sent).
  rules: () => get(`/rules`),
  saveRule: (from_model, to_model) => send(`/rules`, "POST", { from_model, to_model }),
  deleteRule: (from_model) => send(`/rules?from_model=${encodeURIComponent(from_model)}`, "DELETE"),
};
