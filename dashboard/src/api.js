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

export const api = {
  summary: (days = 30) => get(`/summary?days=${days}`),
  spendByModel: (days = 30) => get(`/spend-by-model?days=${days}`),
  recent: (limit = 25) => get(`/recent?limit=${limit}`),
  spendDaily: (days = 30) => get(`/spend-daily?days=${days}`),
  budgets: () => get(`/budgets`),
  suggestions: (days = 30) => get(`/suggestions?days=${days}`),
};
