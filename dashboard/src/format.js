// Formatting helpers. Every helper returns an em dash for null/undefined so the
// UI never shows a fake zero for an unknown value. Numeric formatters only
// accept finite numbers: '', false, 'abc', Infinity all render as a dash too.

const DASH = '—'

function isNil(v) {
  return v === null || v === undefined || Number.isNaN(v)
}

function isNotNumber(v) {
  return typeof v !== 'number' || !Number.isFinite(v)
}

const groupInt = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 })
const compact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 })

// Number of decimals for a USD amount: small per-request costs need more
// precision than monthly totals.
function moneyDecimals(abs) {
  if (abs === 0) return 2
  if (abs >= 1) return 2
  if (abs >= 0.01) return 3
  if (abs >= 0.0001) return 4
  return 6
}

export function money(v) {
  if (isNotNumber(v)) return DASH
  const n = v
  const abs = Math.abs(n)
  const decimals = moneyDecimals(abs)
  const fixed = abs.toFixed(decimals)
  const [intPart, fracPart] = fixed.split('.')
  const grouped = groupInt.format(Number(intPart))
  const sign = n < 0 ? '-' : ''
  return `${sign}$${grouped}.${fracPart}`
}

export function percent(v) {
  if (isNotNumber(v)) return DASH
  return `${(v * 100).toFixed(1)}%`
}

export function compactInt(v) {
  if (isNotNumber(v)) return DASH
  return compact.format(v)
}

export function integer(v) {
  if (isNotNumber(v)) return DASH
  return groupInt.format(v)
}

// The provider a served model name belongs to, as one of a small fixed set:
// 'anthropic' | 'openai' | 'google' | 'nvidia' | 'other'. Mirrors the gateway's
// app.metrics.provider_of routing (claude- / gpt-|o-series / gemini- / a slash = a
// NVIDIA NIM id), collapsed to the labels the dashboard colours by. Used for the
// per-provider dot in Recent calls and the Spend by model table.
export function providerOf(model) {
  if (typeof model !== 'string' || !model) return 'other'
  if (model.startsWith('claude-')) return 'anthropic'
  // OpenAI: gpt-*, or the o-series (leading 'o', a dash, and no slash — a slash is NIM).
  if (model.startsWith('gpt-') || (model.startsWith('o') && model.includes('-') && !model.includes('/'))) return 'openai'
  if (model.startsWith('gemini-')) return 'google'
  if (model.includes('/')) return 'nvidia'
  return 'other'
}

// Human label for a provider key, used as the dot's title/tooltip.
const PROVIDER_LABELS = {
  anthropic: 'Anthropic',
  openai: 'OpenAI',
  google: 'Google',
  nvidia: 'NVIDIA',
  other: 'Other',
}

export function providerLabel(model) {
  return PROVIDER_LABELS[providerOf(model)] || 'Other'
}

// HH:MM:SS in the viewer's local time zone.
export function time(iso) {
  if (isNil(iso)) return DASH
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return DASH
  return d.toLocaleTimeString('en-GB', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
}
