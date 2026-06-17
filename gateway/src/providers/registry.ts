import type { ProviderAdapter, ProviderName } from "./types";
import { openaiAdapter } from "./openai";
import { googleAdapter } from "./google";

/**
 * Phase 8 — model registry + adapter dispatch.
 *
 * `providerForModel` classifies a resolved model id to its provider; the proxy
 * dispatches on that. UNKNOWN models resolve to "anthropic", so today's default
 * behavior is unchanged — only models we explicitly recognize leave the Anthropic
 * path.
 */

// OpenAI model id patterns: gpt-* (gpt-4o, gpt-4.1-mini, ...) and the o-* family
// of reasoning models (o1, o3-mini, o4-mini, ...).
const OPENAI_PATTERNS: ReadonlyArray<RegExp> = [/^gpt[-0-9]/i, /^o\d/i, /^o-/i, /^chatgpt-/i];

// Google Gemini model id patterns: gemini-* (gemini-2.5-flash-lite, ...).
const GOOGLE_PATTERNS: ReadonlyArray<RegExp> = [/^gemini-/i];

export function providerForModel(model: string | null): ProviderName {
  if (!model) return "anthropic";
  if (model.toLowerCase().startsWith("claude-")) return "anthropic";
  for (const pattern of OPENAI_PATTERNS) {
    if (pattern.test(model)) return "openai";
  }
  for (const pattern of GOOGLE_PATTERNS) {
    if (pattern.test(model)) return "google";
  }
  return "anthropic"; // unknown -> Anthropic default path (today's behavior)
}

/**
 * Registered adapters, keyed by provider. Anthropic is intentionally ABSENT: its
 * path stays inline in proxy.ts (streaming + caching unchanged), so only
 * non-Anthropic providers are dispatched through an adapter here.
 */
const ADAPTERS: Partial<Record<ProviderName, ProviderAdapter>> = {
  openai: openaiAdapter,
  google: googleAdapter,
};

/** The adapter for a provider, or null when it is served inline (Anthropic). */
export function getAdapter(provider: ProviderName): ProviderAdapter | null {
  return ADAPTERS[provider] ?? null;
}
