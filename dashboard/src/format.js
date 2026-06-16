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

// opus-4-8 -> "Opus 4.8" ; haiku-4-5 -> "Haiku 4.5"
export function prettyModel(model) {
  const s = shortModel(model);
  if (s === "(unknown)") return s;
  const [name, ...rest] = s.split("-");
  const label = name.charAt(0).toUpperCase() + name.slice(1);
  return rest.length ? `${label} ${rest.join(".")}` : label;
}

// "2026-05-17" -> "May 17" (parsed as UTC midnight so the day never shifts).
export function fmtDay(day) {
  const d = new Date(day + "T00:00:00Z");
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}
