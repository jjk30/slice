// Small display formatters. Dev spend numbers are tiny (fractions of a cent),
// so currency formatting adapts precision to the magnitude instead of always
// showing 2 decimals (which would render everything as "$0.00"). Larger numbers
// get thousands separators, matching the mockup's "$3,420" style.

export function usd(value) {
  const n = Number(value) || 0;
  if (n === 0) return "$0";
  const abs = Math.abs(n);
  let decimals;
  if (abs >= 100) decimals = 0;
  else if (abs >= 1) decimals = 2;
  else if (abs >= 0.01) decimals = 4;
  else decimals = 6;
  return "$" + n.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

export function pct(value, decimals = 0) {
  const n = Number(value) || 0;
  return n.toFixed(decimals) + "%";
}

export function int(value) {
  return (Number(value) || 0).toLocaleString("en-US");
}

// claude-haiku-4-5-20251001 -> haiku-4-5 ; claude-opus-4-8 -> opus-4-8
export function shortModel(model) {
  if (!model) return "(unknown)";
  return String(model)
    .replace(/^claude-/, "")
    .replace(/-\d{8}$/, "");
}

// Explicit display names for the known models — reliable across providers. A
// generic prettifier can't tell a version number ("2.5") from a word suffix
// ("flash-lite"), which is what produced the stray-dot bug ("Gemini 2.5.flash.lite").
const MODEL_NAMES = {
  // Anthropic
  "claude-opus-4-8": "Opus 4.8",
  "claude-sonnet-4-6": "Sonnet 4.6",
  "claude-haiku-4-5-20251001": "Haiku 4.5",
  // OpenAI
  "gpt-4o": "GPT-4o",
  "gpt-4o-mini": "GPT-4o mini",
  "gpt-4.1": "GPT-4.1",
  "gpt-4.1-mini": "GPT-4.1 mini",
  "gpt-4.1-nano": "GPT-4.1 nano",
  o1: "o1",
  "o3-mini": "o3-mini",
  "o4-mini": "o4-mini",
  // Google
  "gemini-2.5-pro": "Gemini 2.5 Pro",
  "gemini-2.5-flash": "Gemini 2.5 Flash",
  "gemini-2.5-flash-lite": "Gemini 2.5 Flash-Lite",
  "gemini-2.0-flash": "Gemini 2.0 Flash",
  "gemini-2.0-flash-lite": "Gemini 2.0 Flash-Lite",
  "gemini-1.5-flash": "Gemini 1.5 Flash",
  "gemini-1.5-pro": "Gemini 1.5 Pro",
};

// claude-opus-4-8 -> "Opus 4.8" ; gemini-2.5-flash-lite -> "Gemini 2.5 Flash-Lite".
// Known models use the explicit table above; anything else falls back to a clean,
// dot-free rendering (title-cased family + space-joined remainder).
export function prettyModel(model) {
  if (!model) return "(unknown)";
  const key = String(model);
  if (MODEL_NAMES[key]) return MODEL_NAMES[key];

  const s = shortModel(key); // strips claude- and any trailing 8-digit date
  if (s === "(unknown)") return s;
  const [head, ...rest] = s.split("-");
  const label = head.charAt(0).toUpperCase() + head.slice(1);
  return rest.length ? `${label} ${rest.join(" ")}` : label;
}

// "2026-05-17" -> "May 17" (parsed as UTC midnight so the day never shifts).
export function fmtDay(day) {
  const d = new Date(day + "T00:00:00Z");
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}
